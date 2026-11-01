<?php
defined( 'ABSPATH' ) || exit;

/**
 * A snapshot is a copy of the plugin/theme folders (and, for core updates, the
 * WordPress files) taken right before an update.
 * The manifest is written before anything is touched, so rescue.php can put
 * the old files back even if the update leaves WordPress unable to start.
 */
final class Sajtvakt_Snapshot {

	/**
	 * @return array{id:string, manifest:array}|WP_Error
	 */
	public static function create( array $plugin_files, array $theme_slugs, $ref = '', $core = false ) {
		Sajtvakt_Core::ensure_data_dir();
		Sajtvakt_Core::cleanup();

		$all_plugins = get_plugins();
		$items       = array();
		$plugin_root = rtrim( wp_normalize_path( WP_PLUGIN_DIR ), '/' );

		foreach ( $plugin_files as $file ) {
			if ( ! isset( $all_plugins[ $file ] ) ) {
				continue;
			}
			$folder  = dirname( $file );
			$single  = ( '.' === $folder );
			$items[] = array(
				'type'           => 'plugin',
				'slug'           => $file,
				'name'           => $all_plugins[ $file ]['Name'],
				'target'         => $plugin_root . '/' . ( $single ? $file : $folder ),
				'stored'         => 'plugin-' . ( $single ? $file : $folder ),
				'version_before' => $all_plugins[ $file ]['Version'],
			);
		}

		foreach ( $theme_slugs as $slug ) {
			$theme = wp_get_theme( $slug );
			if ( ! $theme->exists() ) {
				continue;
			}
			$items[] = array(
				'type'           => 'theme',
				'slug'           => $slug,
				'name'           => $theme->get( 'Name' ),
				'target'         => rtrim( wp_normalize_path( $theme->get_stylesheet_directory() ), '/' ),
				'stored'         => 'theme-' . $slug,
				'version_before' => $theme->get( 'Version' ),
			);
		}

		if ( $core ) {
			$items = array_merge( $items, self::core_items() );
		}

		if ( ! $items ) {
			return new WP_Error( 'nothing', 'Inget att ta ögonblicksbild av.' );
		}

		// Make sure there is room for the copy (with margin).
		$needed = 0;
		foreach ( $items as $item ) {
			$needed += Sajtvakt_Core::dir_size( $item['target'] );
		}
		$free = function_exists( 'disk_free_space' ) ? @disk_free_space( Sajtvakt_Core::data_dir() ) : false; // phpcs:ignore
		if ( false !== $free && null !== $free && $free < $needed * 1.5 + 20 * 1024 * 1024 ) {
			return new WP_Error( 'disk_full', 'För lite diskutrymme för att spara en kopia före uppdateringen.' );
		}

		$id  = Sajtvakt_Core::new_id();
		$dir = Sajtvakt_Core::snapshot_dir( $id );
		if ( ! @mkdir( $dir . '/files', 0755, true ) ) { // phpcs:ignore
			return new WP_Error( 'not_writable', 'Kunde inte skapa mappen för ögonblicksbilden.' );
		}

		$manifest = array(
			'id'        => $id,
			'ref'       => preg_match( '/^[a-f0-9]{16,64}$/', (string) $ref ) ? $ref : '',
			'created'   => time(),
			'status'    => 'copying',
			'wordpress' => $GLOBALS['wp_version'],
			'items'     => $items,
		);
		Sajtvakt_Core::write_json( $dir . '/manifest.php', $manifest );

		foreach ( $items as $item ) {
			if ( ! self::save_copy( $item['target'], $dir . '/files/' . $item['stored'] ) ) {
				$manifest['status'] = 'failed';
				Sajtvakt_Core::write_json( $dir . '/manifest.php', $manifest );
				return new WP_Error( 'copy_failed', 'Kunde inte kopiera ' . $item['name'] . ' före uppdateringen.' );
			}
		}

		$manifest['status'] = 'ready';
		Sajtvakt_Core::write_json( $dir . '/manifest.php', $manifest );

		return array( 'id' => $id, 'manifest' => $manifest );
	}

	/**
	 * WordPress egna filer: wp-admin, wp-includes och filerna i roten – aldrig wp-config.php.
	 */
	private static function core_items() {
		$root  = rtrim( wp_normalize_path( ABSPATH ), '/' );
		$ver   = $GLOBALS['wp_version'];
		$items = array();
		foreach ( array( 'wp-admin', 'wp-includes' ) as $dir ) {
			if ( is_dir( $root . '/' . $dir ) ) {
				$items[] = array(
					'type'           => 'core',
					'slug'           => $dir,
					'name'           => 'WordPress',
					'target'         => $root . '/' . $dir,
					'stored'         => 'core-' . $dir,
					'version_before' => $ver,
				);
			}
		}
		foreach ( (array) scandir( $root ) as $file ) {
			if ( Sajtvakt_Core::is_core_root_file( $file ) && is_file( $root . '/' . $file ) ) {
				$items[] = array(
					'type'           => 'core-file',
					'slug'           => $file,
					'name'           => 'WordPress',
					'single'         => true,
					'target'         => $root . '/' . $file,
					'stored'         => 'core-root-' . $file,
					'version_before' => $ver,
				);
			}
		}
		return $items;
	}

	/**
	 * Save a folder (or single plugin file) as a zip. A zip is not executable,
	 * so old plugin versions are never reachable as PHP over the web.
	 */
	private static function save_copy( $target, $dest_base ) {
		@set_time_limit( 300 ); // phpcs:ignore
		if ( ! class_exists( 'ZipArchive' ) ) {
			return Sajtvakt_Core::rcopy( $target, $dest_base ); // Fallback: plain copy.
		}
		$zip = new ZipArchive();
		if ( true !== $zip->open( $dest_base . '.zip', ZipArchive::CREATE | ZipArchive::OVERWRITE ) ) {
			return false;
		}
		if ( is_file( $target ) ) {
			$zip->addFile( $target, basename( $target ) );
		} else {
			$root = rtrim( wp_normalize_path( $target ), '/' );
			$it   = new RecursiveIteratorIterator(
				new RecursiveDirectoryIterator( $root, FilesystemIterator::SKIP_DOTS | FilesystemIterator::UNIX_PATHS ),
				RecursiveIteratorIterator::SELF_FIRST
			);
			foreach ( $it as $f ) {
				$rel = substr( wp_normalize_path( $f->getPathname() ), strlen( $root ) + 1 );
				if ( $f->isDir() ) {
					$zip->addEmptyDir( $rel );
				} elseif ( $f->isFile() ) {
					$zip->addFile( $f->getPathname(), $rel );
				}
			}
		}
		return $zip->close();
	}

	/** Markera att det som sparades har tagits bort (avinstallerat från panelen). */
	public static function mark_removed( $id ) {
		$manifest = Sajtvakt_Core::read_snapshot( $id );
		if ( ! $manifest ) {
			return;
		}
		foreach ( $manifest['items'] as &$item ) {
			$item['removed']       = true;
			$item['version_after'] = null;
		}
		unset( $item );
		$manifest['status']  = 'updated';
		$manifest['updated'] = time();
		Sajtvakt_Core::write_json( Sajtvakt_Core::snapshot_dir( $id ) . '/manifest.php', $manifest );
	}

	public static function mark_updated( $id, array $versions_after ) {
		$manifest = Sajtvakt_Core::read_snapshot( $id );
		if ( ! $manifest ) {
			return;
		}
		foreach ( $manifest['items'] as &$item ) {
			$key = $item['type'] . ':' . $item['slug'];
			if ( array_key_exists( $key, $versions_after ) ) {
				$item['version_after'] = $versions_after[ $key ];
			} elseif ( in_array( $item['type'], array( 'core', 'core-file' ), true ) && array_key_exists( 'core', $versions_after ) ) {
				$item['version_after'] = $versions_after['core'];
			}
		}
		unset( $item );
		$manifest['status']  = 'updated';
		$manifest['updated'] = time();
		Sajtvakt_Core::write_json( Sajtvakt_Core::snapshot_dir( $id ) . '/manifest.php', $manifest );
	}
}
