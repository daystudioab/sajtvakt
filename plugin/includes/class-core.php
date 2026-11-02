<?php
/**
 * Core helpers that work WITHOUT WordPress loaded.
 *
 * Used both by the plugin and by rescue.php (which must keep working when a
 * broken update has taken WordPress down).
 */

if ( ! defined( 'ABSPATH' ) && ! defined( 'SAJTVAKT_RESCUE' ) ) {
	exit;
}

final class Sajtvakt_Core {

	const VERSION   = '0.6.0';
	const MAX_SKEW  = 300;  // Seconds a signed request may differ from server time.
	const NONCE_TTL = 900;  // Seconds a used nonce is remembered.
	const GUARD     = "<?php exit; ?>\n";

	private static $data_dir = '';

	/* ------------------------------------------------------------------ */
	/* Data directory                                                      */
	/* ------------------------------------------------------------------ */

	public static function set_data_dir( $dir ) {
		self::$data_dir = rtrim( str_replace( '\\', '/', $dir ), '/' );
	}

	public static function data_dir( $sub = '' ) {
		return self::$data_dir . ( '' !== $sub ? '/' . ltrim( $sub, '/' ) : '' );
	}

	public static function ensure_data_dir() {
		$dir = self::$data_dir;
		foreach ( array( $dir, "$dir/jobs", "$dir/backups", "$dir/snapshots", "$dir/trash", "$dir/restores" ) as $d ) {
			if ( ! is_dir( $d ) ) {
				@mkdir( $d, 0755, true ); // phpcs:ignore
			}
			if ( ! file_exists( "$d/index.php" ) ) {
				@file_put_contents( "$d/index.php", "<?php // Tyst.\n" ); // phpcs:ignore
			}
		}
		// Lets the server test from outside whether this folder is readable over the web (nginx ignores .htaccess).
		if ( ! file_exists( "$dir/canary.txt" ) ) {
			@file_put_contents( "$dir/canary.txt", 'sajtvakt-canary' ); // phpcs:ignore
		}
		$htaccess = "# Sajtvakt: ingen direkt åtkomst\n"
			. "<IfModule mod_authz_core.c>\nRequire all denied\n</IfModule>\n"
			. "<IfModule !mod_authz_core.c>\nOrder allow,deny\nDeny from all\n</IfModule>\n";
		$files = array(
			"$dir/.htaccess" => $htaccess,
			"$dir/index.php" => "<?php // Tyst.\n",
			"$dir/web.config" => '<?xml version="1.0"?><configuration><system.webServer><authorization><deny users="*" /></authorization></system.webServer></configuration>',
		);
		foreach ( $files as $path => $content ) {
			if ( ! file_exists( $path ) ) {
				@file_put_contents( $path, $content ); // phpcs:ignore
			}
		}
		return is_dir( $dir ) && is_writable( $dir );
	}

	/* ------------------------------------------------------------------ */
	/* Guarded JSON files (".php" with an exit guard, safe on nginx too)    */
	/* ------------------------------------------------------------------ */

	public static function encode_guarded( $data ) {
		return self::GUARD . json_encode( $data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT );
	}

	public static function decode_guarded( $raw ) {
		if ( ! is_string( $raw ) || '' === $raw ) {
			return null;
		}
		if ( 0 === strpos( $raw, self::GUARD ) ) {
			$raw = substr( $raw, strlen( self::GUARD ) );
		}
		$data = json_decode( $raw, true );
		return is_array( $data ) ? $data : null;
	}

	public static function read_json( $file ) {
		if ( ! is_readable( $file ) ) {
			return null;
		}
		return self::decode_guarded( (string) file_get_contents( $file ) );
	}

	public static function write_json( $file, $data ) {
		$tmp = $file . '.tmp-' . bin2hex( random_bytes( 4 ) );
		if ( false === file_put_contents( $tmp, self::encode_guarded( $data ), LOCK_EX ) ) {
			return false;
		}
		return rename( $tmp, $file );
	}

	/* ------------------------------------------------------------------ */
	/* Connection key                                                      */
	/* ------------------------------------------------------------------ */

	public static function get_key() {
		$data = self::read_json( self::data_dir( 'key.php' ) );
		return ( is_array( $data ) && ! empty( $data['key'] ) ) ? (string) $data['key'] : null;
	}

	public static function generate_key() {
		self::ensure_data_dir();
		$key = bin2hex( random_bytes( 32 ) );
		self::write_json( self::data_dir( 'key.php' ), array( 'key' => $key, 'created' => time() ) );
		return $key;
	}

	public static function read_paths() {
		return self::read_json( self::data_dir( 'paths.php' ) );
	}

	public static function write_paths( $paths ) {
		return self::write_json( self::data_dir( 'paths.php' ), $paths );
	}

	/* ------------------------------------------------------------------ */
	/* Request signing                                                     */
	/* ------------------------------------------------------------------ */

	/**
	 * What gets signed: operation, time, nonce, the optional params header and
	 * a hash of the body (JSON for most calls, raw bytes for uploads).
	 */
	public static function string_to_sign( $op, $timestamp, $nonce, $params, $body ) {
		return "sajtvakt-v1\n{$op}\n{$timestamp}\n{$nonce}\n" . hash( 'sha256', (string) $params ) . "\n" . hash( 'sha256', (string) $body );
	}

	public static function request_headers() {
		$get = function ( $name ) {
			$key = 'HTTP_X_SAJTVAKT_' . strtoupper( $name );
			return isset( $_SERVER[ $key ] ) ? trim( (string) $_SERVER[ $key ] ) : '';
		};
		return array(
			'timestamp' => $get( 'timestamp' ),
			'nonce'     => $get( 'nonce' ),
			'signature' => $get( 'signature' ),
			'params'    => isset( $_SERVER['HTTP_X_SAJTVAKT_PARAMS'] ) ? (string) $_SERVER['HTTP_X_SAJTVAKT_PARAMS'] : '',
		);
	}

	/**
	 * @return true|string True when valid, otherwise an error code.
	 */
	public static function verify_request( $op, $body, array $headers ) {
		$key = self::get_key();
		if ( ! $key ) {
			return 'not_configured';
		}
		$ts    = $headers['timestamp'];
		$nonce = $headers['nonce'];
		$sig   = strtolower( $headers['signature'] );

		if ( '' === $ts || ! ctype_digit( $ts ) || abs( time() - (int) $ts ) > self::MAX_SKEW ) {
			return 'bad_timestamp';
		}
		if ( ! preg_match( '/^[A-Za-z0-9_-]{16,64}$/', $nonce ) ) {
			return 'bad_nonce';
		}
		$expected = hash_hmac( 'sha256', self::string_to_sign( $op, $ts, $nonce, $headers['params'], $body ), $key );
		if ( ! hash_equals( $expected, $sig ) ) {
			return 'bad_signature';
		}
		if ( ! self::remember_nonce( $nonce ) ) {
			return 'replay';
		}
		return true;
	}

	private static function remember_nonce( $nonce ) {
		$file = self::data_dir( 'nonces.php' );
		$fh   = @fopen( $file, 'c+' ); // phpcs:ignore
		if ( ! $fh ) {
			return true; // Cannot persist; the timestamp window still limits replays.
		}
		flock( $fh, LOCK_EX );
		$list = self::decode_guarded( stream_get_contents( $fh ) );
		$list = is_array( $list ) ? $list : array();
		$now  = time();
		foreach ( $list as $n => $t ) {
			if ( $t < $now - self::NONCE_TTL ) {
				unset( $list[ $n ] );
			}
		}
		$fresh = ! isset( $list[ $nonce ] );
		if ( $fresh ) {
			$list[ $nonce ] = $now;
			ftruncate( $fh, 0 );
			rewind( $fh );
			fwrite( $fh, self::encode_guarded( $list ) );
			fflush( $fh );
		}
		flock( $fh, LOCK_UN );
		fclose( $fh );
		return $fresh;
	}

	/* ------------------------------------------------------------------ */
	/* Output                                                              */
	/* ------------------------------------------------------------------ */

	public static function send_json( $data, $code = 200 ) {
		while ( ob_get_level() > 0 ) {
			ob_end_clean();
		}
		if ( ! headers_sent() ) {
			http_response_code( $code );
			header( 'Content-Type: application/json; charset=utf-8' );
			header( 'Cache-Control: no-store, no-cache, must-revalidate, max-age=0' );
			header( 'X-Sajtvakt: ' . self::VERSION );
		}
		echo json_encode( $data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE );
		exit;
	}

	public static function send_error( $code, $message, $http = 400, $extra = array() ) {
		self::send_json( array_merge( array( 'ok' => false, 'error' => $code, 'message' => $message ), $extra ), $http );
	}

	/* ------------------------------------------------------------------ */
	/* File helpers                                                        */
	/* ------------------------------------------------------------------ */

	public static function rcopy( $src, $dst ) {
		if ( is_link( $src ) ) {
			return @symlink( readlink( $src ), $dst ); // phpcs:ignore
		}
		if ( is_file( $src ) ) {
			return copy( $src, $dst );
		}
		if ( ! is_dir( $dst ) && ! @mkdir( $dst, 0755, true ) ) { // phpcs:ignore
			return false;
		}
		$ok = true;
		foreach ( (array) scandir( $src ) as $entry ) {
			if ( '.' === $entry || '..' === $entry ) {
				continue;
			}
			$ok = self::rcopy( "$src/$entry", "$dst/$entry" ) && $ok;
		}
		return $ok;
	}

	public static function rrmdir( $path ) {
		if ( is_link( $path ) || is_file( $path ) ) {
			return @unlink( $path ); // phpcs:ignore
		}
		if ( ! is_dir( $path ) ) {
			return true;
		}
		foreach ( (array) scandir( $path ) as $entry ) {
			if ( '.' === $entry || '..' === $entry ) {
				continue;
			}
			self::rrmdir( "$path/$entry" );
		}
		return @rmdir( $path ); // phpcs:ignore
	}

	public static function dir_size( $path ) {
		if ( is_file( $path ) ) {
			return (int) filesize( $path );
		}
		$size = 0;
		if ( is_dir( $path ) ) {
			$it = new RecursiveIteratorIterator( new RecursiveDirectoryIterator( $path, FilesystemIterator::SKIP_DOTS ) );
			foreach ( $it as $f ) {
				$size += $f->isFile() ? $f->getSize() : 0;
			}
		}
		return $size;
	}

	public static function valid_id( $id ) {
		return is_string( $id ) && (bool) preg_match( '/^[a-f0-9]{8,64}$/', $id );
	}

	public static function new_id() {
		return gmdate( 'Ymd-His' ) . '-' . bin2hex( random_bytes( 6 ) );
	}

	public static function valid_stamped_id( $id ) {
		return is_string( $id ) && (bool) preg_match( '/^\d{8}-\d{6}-[a-f0-9]{12}$/', $id );
	}

	/* ------------------------------------------------------------------ */
	/* Snapshot rollback (pure PHP, used by plugin and rescue)             */
	/* ------------------------------------------------------------------ */

	public static function snapshot_dir( $id ) {
		return self::data_dir( 'snapshots/' . $id );
	}

	public static function read_snapshot( $id ) {
		if ( ! self::valid_stamped_id( $id ) ) {
			return null;
		}
		return self::read_json( self::snapshot_dir( $id ) . '/manifest.php' );
	}

	public static function list_snapshots() {
		$out  = array();
		$base = self::data_dir( 'snapshots' );
		if ( ! is_dir( $base ) ) {
			return $out;
		}
		foreach ( (array) scandir( $base ) as $id ) {
			if ( ! self::valid_stamped_id( $id ) ) {
				continue;
			}
			$m = self::read_snapshot( $id );
			if ( ! $m ) {
				continue;
			}
			$items    = array();
			$has_core = false;
			foreach ( (array) $m['items'] as $item ) {
				// WordPress-filerna visas som en rad.
				if ( 'core-file' === $item['type'] || ( 'core' === $item['type'] && $has_core ) ) {
					continue;
				}
				$has_core = $has_core || 'core' === $item['type'];
				$items[]  = array(
					'type'   => $item['type'],
					'slug'   => $item['slug'],
					'name'   => isset( $item['name'] ) ? $item['name'] : $item['slug'],
					'before' => isset( $item['version_before'] ) ? $item['version_before'] : null,
					'after'  => isset( $item['version_after'] ) ? $item['version_after'] : null,
					'removed' => ! empty( $item['removed'] ),
				);
			}
			$out[] = array(
				'id'          => $id,
				'created'     => $m['created'],
				'status'      => $m['status'],
				'rolled_back' => isset( $m['rolled_back'] ) ? $m['rolled_back'] : null,
				'items'       => $items,
			);
		}
		usort( $out, function ( $a, $b ) {
			return strcmp( $b['id'], $a['id'] );
		} );
		return $out;
	}

	/**
	 * Is $target inside one of the directories we are allowed to restore into?
	 */
	private static function target_allowed( $target ) {
		$paths = self::read_paths();
		if ( ! $paths ) {
			return false;
		}
		$target  = rtrim( str_replace( '\\', '/', $target ), '/' );
		// WordPress egna filer (vid återställning av en kärnuppdatering) – exakt dessa, inget annat i roten.
		if ( ! empty( $paths['abspath'] ) ) {
			$abs = rtrim( str_replace( '\\', '/', $paths['abspath'] ), '/' );
			if ( $target === $abs . '/wp-admin' || $target === $abs . '/wp-includes' ) {
				return true;
			}
			if ( dirname( $target ) === $abs && self::is_core_root_file( basename( $target ) ) ) {
				return true;
			}
		}
		$allowed = array_merge( array( $paths['plugin_dir'] ), (array) $paths['theme_roots'] );
		foreach ( $allowed as $root ) {
			$root = rtrim( str_replace( '\\', '/', $root ), '/' );
			if ( '' !== $root && 0 === strpos( $target, $root . '/' ) && false === strpos( substr( $target, strlen( $root ) ), '..' ) ) {
				return true;
			}
		}
		return false;
	}

	/** Filer i WordPress-roten som hör till WordPress själv. wp-config.php räknas aldrig hit. */
	public static function is_core_root_file( $file ) {
		if ( 'wp-config.php' === $file ) {
			return false;
		}
		return (bool) preg_match( '/^(index\.php|xmlrpc\.php|wp-[a-z0-9-]+\.php|license\.txt|readme\.html)$/', (string) $file );
	}

	/** Find the snapshot that belongs to a server-generated reference. */
	public static function snapshot_by_ref( $ref ) {
		if ( ! is_string( $ref ) || ! preg_match( '/^[a-f0-9]{16,64}$/', $ref ) ) {
			return null;
		}
		foreach ( glob( self::data_dir( 'snapshots/*/manifest.php' ) ) ?: array() as $file ) {
			$m = self::read_json( $file );
			if ( $m && isset( $m['ref'] ) && hash_equals( $m['ref'], $ref ) ) {
				return $m['id'];
			}
		}
		return null;
	}

	/**
	 * Unpack a saved copy next to the live folder (same filesystem), so the
	 * swap itself is two quick renames instead of a long copy.
	 */
	private static function unpack_saved( $id, array $item, $staging ) {
		$base = self::snapshot_dir( $id ) . '/files/' . $item['stored'];
		if ( is_file( $base . '.zip' ) && class_exists( 'ZipArchive' ) ) {
			$zip = new ZipArchive();
			if ( true !== $zip->open( $base . '.zip' ) ) {
				return false;
			}
			$ok = @mkdir( $staging, 0755, true ) && $zip->extractTo( $staging ); // phpcs:ignore
			$zip->close();
			return $ok;
		}
		if ( file_exists( $base ) ) { // Older snapshot format: a plain copy.
			return self::rcopy( $base, $staging );
		}
		return false;
	}

	/**
	 * Put the files saved in a snapshot back in place.
	 *
	 * @return array{ok:bool, items:array, message:string}
	 */
	public static function rollback_snapshot( $id ) {
		$manifest = self::read_snapshot( $id );
		if ( ! $manifest ) {
			return array( 'ok' => false, 'items' => array(), 'message' => 'Ögonblicksbilden finns inte.' );
		}
		if ( 'ready' !== $manifest['status'] && 'updated' !== $manifest['status'] ) {
			return array( 'ok' => false, 'items' => array(), 'message' => 'Ögonblicksbilden är ofullständig och kan inte användas.' );
		}

		@set_time_limit( 300 ); // phpcs:ignore
		ignore_user_abort( true );

		$trash = self::data_dir( 'trash/' . $id . '-' . time() );
		@mkdir( $trash, 0755, true ); // phpcs:ignore

		$results = array();
		$all_ok  = true;
		foreach ( (array) $manifest['items'] as $item ) {
			$target = rtrim( $item['target'], '/' );
			$single = ! empty( $item['single'] ) || ( 'plugin' === $item['type'] && false === strpos( $item['slug'], '/' ) );
			$res    = array( 'type' => $item['type'], 'slug' => $item['slug'], 'ok' => false );

			if ( ! self::target_allowed( $target ) ) {
				$res['message'] = 'Otillåten sökväg.';
				$all_ok         = false;
				$results[]      = $res;
				continue;
			}

			// 1. Unpack the old version beside the live one (the slow part, nothing live is touched yet).
			$staging = dirname( $target ) . '/.sajtvakt-rollback-' . bin2hex( random_bytes( 4 ) );
			if ( ! self::unpack_saved( $id, $item, $staging ) ) {
				self::rrmdir( $staging );
				$res['message'] = 'Den sparade kopian kunde inte packas upp.';
				$all_ok         = false;
				$results[]      = $res;
				continue;
			}
			$new = $single ? $staging . '/' . basename( $target ) : $staging;

			// 2. Swap with two renames. If the second fails, put the live one back.
			$parked = $trash . '/' . $item['stored'];
			$had    = file_exists( $target ) || is_link( $target );
			if ( $had && ! @rename( $target, $parked ) ) { // phpcs:ignore
				self::rrmdir( $staging );
				$res['message'] = 'Kunde inte flytta undan den nuvarande versionen (rättigheter?).';
				$all_ok         = false;
				$results[]      = $res;
				continue;
			}
			if ( ! @rename( $new, $target ) ) { // phpcs:ignore
				if ( $had ) {
					@rename( $parked, $target ); // phpcs:ignore
				}
				self::rrmdir( $staging );
				$res['message'] = 'Kunde inte lägga tillbaka den gamla versionen.';
				$all_ok         = false;
				$results[]      = $res;
				continue;
			}
			if ( $single ) {
				self::rrmdir( $staging );
			}
			$res['ok'] = true;
			$results[] = $res;
		}

		// Old copies in trash may be executable PHP – do not leave them lying around.
		self::rrmdir( $trash );
		self::clear_maintenance();
		if ( function_exists( 'opcache_reset' ) ) {
			@opcache_reset(); // phpcs:ignore
		}

		$manifest['rolled_back'] = time();
		self::write_json( self::snapshot_dir( $id ) . '/manifest.php', $manifest );

		return array(
			'ok'      => $all_ok,
			'items'   => $results,
			'message' => $all_ok ? 'Återställt.' : 'Återställningen gick bara delvis.',
		);
	}

	public static function clear_maintenance() {
		$paths = self::read_paths();
		if ( $paths && ! empty( $paths['abspath'] ) ) {
			$file = rtrim( $paths['abspath'], '/' ) . '/.maintenance';
			if ( file_exists( $file ) ) {
				return @unlink( $file ); // phpcs:ignore
			}
		}
		return false;
	}

	/* ------------------------------------------------------------------ */
	/* Housekeeping                                                        */
	/* ------------------------------------------------------------------ */

	public static function cleanup() {
		$now = time();

		// Unfinished/forgotten backup jobs: 48 hours.
		foreach ( glob( self::data_dir( 'jobs/*.php' ) ) ?: array() as $file ) {
			$job = self::read_json( $file );
			if ( ! $job || ( $now - (int) $job['updated'] ) > 2 * DAY_SECONDS_FALLBACK ) {
				if ( $job && ! empty( $job['id'] ) && self::valid_id( $job['id'] ) ) {
					self::rrmdir( self::data_dir( 'backups/' . $job['id'] ) );
				}
				@unlink( $file ); // phpcs:ignore
			}
		}

		// Abandoned restore jobs: 48 hours (temporary tables are dropped by the next start).
		foreach ( glob( self::data_dir( 'restores/*' ), GLOB_ONLYDIR ) ?: array() as $dir ) {
			if ( ( $now - (int) @filemtime( $dir . '/job.php' ) ) > 2 * DAY_SECONDS_FALLBACK ) { // phpcs:ignore
				self::rrmdir( $dir );
			}
		}

		// Trash (should normally be empty already): 1 day.
		foreach ( glob( self::data_dir( 'trash/*' ), GLOB_ONLYDIR ) ?: array() as $dir ) {
			if ( ( $now - (int) filemtime( $dir ) ) > DAY_SECONDS_FALLBACK ) {
				self::rrmdir( $dir );
			}
		}

		// Snapshots: keep the 3 newest always; drop others older than 30 days or beyond 10.
		$snaps = array();
		foreach ( glob( self::data_dir( 'snapshots/*' ), GLOB_ONLYDIR ) ?: array() as $dir ) {
			if ( self::valid_stamped_id( basename( $dir ) ) ) {
				$snaps[] = $dir;
			}
		}
		rsort( $snaps );
		foreach ( $snaps as $i => $dir ) {
			if ( $i < 3 ) {
				continue;
			}
			if ( $i >= 10 || ( $now - (int) filemtime( $dir ) ) > 30 * DAY_SECONDS_FALLBACK ) {
				self::rrmdir( $dir );
			}
		}
	}
}

if ( ! defined( 'DAY_SECONDS_FALLBACK' ) ) {
	define( 'DAY_SECONDS_FALLBACK', 86400 );
}
