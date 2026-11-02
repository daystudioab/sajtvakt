<?php
defined( 'ABSPATH' ) || exit;

/**
 * Everything the server needs to know about the site in one call.
 */
final class Sajtvakt_Status {

	public static function load_admin_includes() {
		require_once ABSPATH . 'wp-admin/includes/file.php';
		require_once ABSPATH . 'wp-admin/includes/misc.php';
		require_once ABSPATH . 'wp-admin/includes/plugin.php';
		require_once ABSPATH . 'wp-admin/includes/theme.php';
		require_once ABSPATH . 'wp-admin/includes/update.php';
		require_once ABSPATH . 'wp-admin/includes/translation-install.php';
	}

	public static function refresh_update_data() {
		self::load_admin_includes();
		wp_clean_update_cache();
		wp_version_check( array(), true );
		Sajtvakt_Self_Update::latest( true );
		wp_update_plugins();
		wp_update_themes();
	}

	public static function collect( $refresh = false ) {
		global $wpdb, $wp_version;

		self::load_admin_includes();
		if ( $refresh ) {
			self::refresh_update_data();
		}

		$plugin_updates = get_site_transient( 'update_plugins' );
		$theme_updates  = get_site_transient( 'update_themes' );

		$plugins = array();
		foreach ( get_plugins() as $file => $data ) {
			$update = null;
			if ( isset( $plugin_updates->response[ $file ] ) ) {
				$u      = $plugin_updates->response[ $file ];
				$update = array(
					'version' => isset( $u->new_version ) ? $u->new_version : null,
					'package' => ! empty( $u->package ),
				);
			}
			$plugins[] = array(
				'file'    => $file,
				'name'    => $data['Name'],
				'version' => $data['Version'],
				'active'  => is_plugin_active( $file ),
				'update'  => $update,
			);
		}

		$themes     = array();
		$stylesheet = get_stylesheet();
		$template   = get_template();
		foreach ( wp_get_themes() as $slug => $theme ) {
			$update = null;
			if ( isset( $theme_updates->response[ $slug ] ) ) {
				$u      = (array) $theme_updates->response[ $slug ];
				$update = array(
					'version' => isset( $u['new_version'] ) ? $u['new_version'] : null,
					'package' => ! empty( $u['package'] ),
				);
			}
			$themes[] = array(
				'slug'    => $slug,
				'name'    => $theme->get( 'Name' ),
				'version' => $theme->get( 'Version' ),
				'active'  => ( $slug === $stylesheet ),
				'parent'  => ( $slug === $template && $template !== $stylesheet ),
				'update'  => $update,
			);
		}

		$core_update = null;
		foreach ( (array) get_core_updates() as $u ) {
			if ( isset( $u->response ) && 'upgrade' === $u->response ) {
				$core_update = array( 'version' => $u->current, 'locale' => $u->locale );
				break;
			}
		}

		$fs_method = get_filesystem_method( array(), WP_CONTENT_DIR );

		$last_backup = get_option( 'sajtvakt_last_backup' );

		return array(
			'ok'       => true,
			'site'     => array(
				'name'      => get_bloginfo( 'name' ),
				'home'      => home_url( '/' ),
				'siteurl'   => site_url( '/' ),
				'multisite' => is_multisite(),
				'language'  => get_locale(),
				'public'    => (bool) get_option( 'blog_public' ),
			),
			'versions' => array(
				'wordpress' => $wp_version,
				'php'       => PHP_VERSION,
				'database'  => $wpdb->db_version(),
				'sajtvakt'  => Sajtvakt_Core::VERSION,
			),
			'updates'  => array(
				'core'         => $core_update,
				'plugins'      => count( array_filter( $plugins, function ( $p ) { return null !== $p['update']; } ) ),
				'themes'       => count( array_filter( $themes, function ( $t ) { return null !== $t['update']; } ) ),
				'translations' => count( (array) wp_get_translation_updates() ),
			),
			'plugins'  => $plugins,
			'themes'   => $themes,
			'security' => Sajtvakt_Security::status(),
			'environment' => array(
				'can_update'         => ( 'direct' === $fs_method ),
				'filesystem_method'  => $fs_method,
				'zip'                => class_exists( 'ZipArchive' ),
				'disk_free'          => function_exists( 'disk_free_space' ) ? @disk_free_space( WP_CONTENT_DIR ) : null, // phpcs:ignore
				'memory_limit'       => ini_get( 'memory_limit' ),
				'max_execution_time' => (int) ini_get( 'max_execution_time' ),
				'data_dir_writable'  => is_writable( Sajtvakt_Core::data_dir() ),
			),
			'sajtvakt' => array(
				'last_backup' => $last_backup ? $last_backup : null,
				'snapshots'   => count( Sajtvakt_Core::list_snapshots() ),
				'canary'      => content_url( 'sajtvakt-data/canary.txt' ),
			),
		);
	}
}
