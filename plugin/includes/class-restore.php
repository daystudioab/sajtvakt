<?php
defined( 'ABSPATH' ) || exit;

/**
 * "Push" a backup to this site – either to restore it, or to clone/move
 * another site here (mode "migrate", with automatic address change).
 *
 * Flow driven by the server:
 *   restore_start  → restore_upload (chunks) … → restore_step … → done
 *
 * All slow work happens off to the side: files are unpacked into a staging
 * folder and the database is imported into temporary tables. Only at the end
 * ("commit") is everything switched in, so a failure half-way leaves the live
 * site untouched. The Sajtvakt plugin itself and its connection key are kept.
 */
final class Sajtvakt_Restore {

	const IMPORT_MAX_STATEMENT = 64 * 1024 * 1024;

	/** Drop-ins that are tied to the old server and must not follow a move. */
	private static $host_specific = array(
		'wp-content/advanced-cache.php', 'wp-content/object-cache.php', 'wp-content/db.php',
		'wp-content/wp-cache-config.php', 'root/.user.ini',
	);

	/* ------------------------------------------------------------------ */
	/* Job storage                                                         */
	/* ------------------------------------------------------------------ */

	private static function dir( $id, $sub = '' ) {
		return Sajtvakt_Core::data_dir( 'restores/' . $id . ( '' !== $sub ? '/' . $sub : '' ) );
	}

	private static function load( $id ) {
		if ( ! Sajtvakt_Core::valid_id( (string) $id ) ) {
			return null;
		}
		return Sajtvakt_Core::read_json( self::dir( $id, 'job.php' ) );
	}

	private static function save( array $job ) {
		$job['updated'] = time();
		Sajtvakt_Core::write_json( self::dir( $job['id'], 'job.php' ), $job );
		return $job;
	}

	private static function view( array $job ) {
		$received = 0;
		$total    = 0;
		foreach ( $job['files'] as $f ) {
			$received += $f['received'];
			$total    += $f['size'];
		}
		return array(
			'ok'       => 'error' !== $job['status'],
			'job'      => $job['id'],
			'mode'     => $job['mode'],
			'status'   => $job['status'],
			'phase'    => $job['phase'],
			'upload'   => array( 'received' => $received, 'total' => $total ),
			'progress' => isset( $job['progress'] ) ? $job['progress'] : null,
			'result'   => isset( $job['result'] ) ? $job['result'] : null,
			'error'    => $job['error'],
		);
	}

	private static function normalize_url( $url ) {
		return untrailingslashit( preg_replace( '#^https?:#i', '', trim( (string) $url ) ) );
	}

	public static function chunk_size() {
		$limit = wp_convert_hr_to_bytes( ini_get( 'post_max_size' ) );
		$size  = 8 * 1024 * 1024;
		if ( $limit > 0 ) {
			$size = min( $size, (int) floor( $limit * 0.8 ) );
		}
		return max( 256 * 1024, $size );
	}

	/* ------------------------------------------------------------------ */
	/* Start                                                               */
	/* ------------------------------------------------------------------ */

	public static function start( array $params ) {
		global $wpdb;

		Sajtvakt_Core::ensure_data_dir();
		Sajtvakt_Core::cleanup();

		$manifest = isset( $params['manifest'] ) && is_array( $params['manifest'] ) ? $params['manifest'] : null;
		$mode     = isset( $params['mode'] ) && 'migrate' === $params['mode'] ? 'migrate' : 'restore';

		if ( ! $manifest || ! isset( $manifest['format'] ) || 'sajtvakt-backup-1' !== $manifest['format'] || empty( $manifest['outputs'] ) ) {
			Sajtvakt_Core::send_error( 'bad_manifest', 'Backupens innehållsförteckning saknas eller är felaktig.', 400 );
		}

		$same_site = self::normalize_url( $manifest['site'] ) === self::normalize_url( home_url( '/' ) );
		if ( 'restore' === $mode && ! $same_site ) {
			Sajtvakt_Core::send_error(
				'url_mismatch',
				'Backupen kommer från ' . $manifest['site'] . ' men den här sajten är ' . home_url( '/' ) . '. Använd läget "flytta" för att lägga den här.',
				409
			);
		}
		if ( is_multisite() ) {
			Sajtvakt_Core::send_error( 'multisite', 'Återställning stöds inte på multisite ännu.', 400 );
		}

		$files = array();
		$total = 0;
		foreach ( $manifest['outputs'] as $o ) {
			if ( ! isset( $o['name'], $o['size'], $o['sha256'] ) || ! preg_match( '/^(database\.sql\.gz|files-\d{3}\.zip)$/', $o['name'] ) ) {
				continue;
			}
			$files[ $o['name'] ] = array( 'size' => (int) $o['size'], 'sha256' => strtolower( $o['sha256'] ), 'received' => 0, 'verified' => false );
			$total              += (int) $o['size'];
		}
		if ( ! $files ) {
			Sajtvakt_Core::send_error( 'empty_backup', 'Backupen innehåller inga filer att återställa.', 400 );
		}

		// Room for: the upload, the unpacked files and the uncompressed database.
		$free = function_exists( 'disk_free_space' ) ? @disk_free_space( Sajtvakt_Core::data_dir() ) : false; // phpcs:ignore
		if ( false !== $free && null !== $free && $free < $total * 3 ) {
			Sajtvakt_Core::send_error(
				'disk_full',
				'För lite diskutrymme. Backupen är ' . size_format( $total ) . ' och behöver ungefär ' . size_format( $total * 3 ) . ' ledigt under återställningen (finns ' . size_format( $free ) . ').',
				507
			);
		}

		// Leftover temporary tables from an abandoned restore are removed,
		// unless another restore is actively running right now.
		$busy = false;
		foreach ( glob( Sajtvakt_Core::data_dir( 'restores/*/job.php' ) ) ?: array() as $file ) {
			$other = Sajtvakt_Core::read_json( $file );
			if ( $other && 'running' === $other['status'] && ( time() - (int) $other['updated'] ) < 2 * HOUR_IN_SECONDS ) {
				$busy = true;
			}
		}
		if ( $busy ) {
			Sajtvakt_Core::send_error( 'busy', 'En annan återställning pågår redan på den här sajten.', 409 );
		}
		self::drop_tables( 'svtmp' );
		self::drop_tables( 'svold' );

		$id = bin2hex( random_bytes( 16 ) );
		foreach ( array( '', 'incoming', 'extract' ) as $sub ) {
			if ( ! is_dir( self::dir( $id, $sub ) ) && ! @mkdir( self::dir( $id, $sub ), 0755, true ) ) { // phpcs:ignore
				Sajtvakt_Core::send_error( 'not_writable', 'Kunde inte skapa arbetsmappen för återställningen.', 500 );
			}
		}
		@file_put_contents( self::dir( $id, 'index.php' ), "<?php // Tyst.\n" ); // phpcs:ignore

		$job = array(
			'id'          => $id,
			'created'     => time(),
			'updated'     => time(),
			'mode'        => $mode,
			'status'      => 'running',
			'phase'       => 'upload',
			'manifest'    => array(
				'site'    => $manifest['site'],
				'siteurl' => isset( $manifest['siteurl'] ) ? $manifest['siteurl'] : $manifest['site'],
				'abspath' => isset( $manifest['abspath'] ) ? $manifest['abspath'] : '',
				'prefix'  => isset( $manifest['prefix'] ) ? $manifest['prefix'] : 'wp_',
			),
			'files'       => $files,
			'tmp_prefix'  => 'svtmp' . bin2hex( random_bytes( 2 ) ) . '_',
			'target'      => array( 'home' => get_option( 'home' ), 'siteurl' => get_option( 'siteurl' ) ),
			'progress'    => null,
			'error'       => null,
			'state'       => array(),
		);
		$job = self::save( $job );

		$out               = self::view( $job );
		$out['chunk_size'] = self::chunk_size();
		return $out;
	}

	/* ------------------------------------------------------------------ */
	/* Upload (raw bytes in the body, params in the signed header)         */
	/* ------------------------------------------------------------------ */

	public static function upload( array $params, $body ) {
		$job = self::load( isset( $params['job'] ) ? $params['job'] : '' );
		if ( ! $job ) {
			Sajtvakt_Core::send_error( 'no_job', 'Återställningsjobbet finns inte.', 404 );
		}
		$name = isset( $params['file'] ) ? (string) $params['file'] : '';
		if ( 'upload' !== $job['phase'] || ! isset( $job['files'][ $name ] ) ) {
			Sajtvakt_Core::send_error( 'bad_file', 'Filen hör inte till den här återställningen.', 400 );
		}
		$f      = $job['files'][ $name ];
		$offset = isset( $params['offset'] ) ? (int) $params['offset'] : -1;
		$path   = self::dir( $job['id'], 'incoming/' . $name );
		clearstatcache( true, $path );
		$have = is_file( $path ) ? (int) filesize( $path ) : 0;

		if ( $offset !== $have ) {
			Sajtvakt_Core::send_error( 'bad_offset', 'Fel position i filen.', 409, array( 'expected' => $have ) );
		}
		if ( $have + strlen( $body ) > $f['size'] ) {
			Sajtvakt_Core::send_error( 'too_much', 'Mer data än filen ska innehålla.', 400, array( 'expected' => $have ) );
		}
		if ( false === file_put_contents( $path, $body, FILE_APPEND | LOCK_EX ) ) {
			Sajtvakt_Core::send_error( 'write_failed', 'Kunde inte spara uppladdningen (fullt på disken?).', 507 );
		}

		// Re-read the job: parallel uploads of different files must not overwrite each other.
		$job                              = self::load( $job['id'] );
		$job['files'][ $name ]['received'] = $have + strlen( $body );
		$job                              = self::save( $job );

		return array( 'ok' => true, 'file' => $name, 'received' => $job['files'][ $name ]['received'], 'size' => $f['size'] );
	}

	/* ------------------------------------------------------------------ */
	/* Steps                                                               */
	/* ------------------------------------------------------------------ */

	public static function step( $id, $budget ) {
		$job = self::load( $id );
		if ( ! $job ) {
			Sajtvakt_Core::send_error( 'no_job', 'Återställningsjobbet finns inte.', 404 );
		}
		if ( 'running' !== $job['status'] ) {
			return self::view( $job );
		}

		$lock = fopen( self::dir( $id, 'job.lock' ), 'c' );
		if ( ! $lock || ! flock( $lock, LOCK_EX | LOCK_NB ) ) {
			Sajtvakt_Core::send_error( 'busy', 'Ett steg körs redan för den här återställningen.', 409 );
		}

		$budget   = max( 3, min( 60, (int) $budget ) );
		$deadline = microtime( true ) + $budget;
		@set_time_limit( $budget + 300 ); // phpcs:ignore
		ignore_user_abort( true );

		try {
			while ( 'running' === $job['status'] && microtime( true ) < $deadline ) {
				$method = 'phase_' . $job['phase'];
				if ( ! method_exists( __CLASS__, $method ) ) {
					throw new Exception( 'Okänd fas: ' . $job['phase'] );
				}
				$job = self::$method( $job, $deadline );
				$job = self::save( $job );
			}
		} catch ( Throwable $e ) {
			if ( in_array( $job['phase'], array( 'merge', 'finalize' ), true ) ) {
				// Already switched in: never give up half-way. Try again in the next step (a few times).
				$job['state']['retries'] = isset( $job['state']['retries'] ) ? $job['state']['retries'] + 1 : 1;
				$job['progress']         = array( 'text' => 'Försöker igen: ' . $e->getMessage() );
				if ( $job['state']['retries'] > 5 ) {
					$job['status'] = 'error';
					$job['error']  = 'Återställningen är inbytt men slutstegen misslyckades: ' . $e->getMessage();
				}
				$job = self::save( $job );
			} else {
				$job['status']               = 'error';
				$job['error']                = $e->getMessage();
				$job['state']['undo_failed'] = self::$undo_failed;
				$job                         = self::save( $job );
				if ( ! self::$undo_failed ) {
					self::drop_tables( $job['tmp_prefix'] );
				}
			}
		}

		flock( $lock, LOCK_UN );
		fclose( $lock );
		return self::view( $job );
	}

	/* -- 1. Verify uploads --------------------------------------------- */

	private static function phase_upload( array $job, $deadline ) {
		foreach ( $job['files'] as $name => $f ) {
			if ( $f['received'] < $f['size'] ) {
				throw new Exception( "Filen {$name} är inte färdiguppladdad (" . $f['received'] . ' av ' . $f['size'] . ' byte).' );
			}
		}
		foreach ( $job['files'] as $name => $f ) {
			if ( $f['verified'] ) {
				continue;
			}
			if ( microtime( true ) > $deadline ) {
				return $job;
			}
			$hash = hash_file( 'sha256', self::dir( $job['id'], 'incoming/' . $name ) );
			if ( ! hash_equals( $f['sha256'], (string) $hash ) ) {
				throw new Exception( "Kontrollsumman stämmer inte för {$name} – filen skadades på vägen. Starta om." );
			}
			$job['files'][ $name ]['verified'] = true;
		}
		$job['phase']    = isset( $job['files']['database.sql.gz'] ) ? 'gunzip' : 'extract';
		$job['progress'] = array( 'text' => 'Uppladdningen är kontrollerad.' );
		return $job;
	}

	/* -- 2. Database: unpack, import to temporary tables, rewrite ------- */

	/**
	 * Unpack database.sql.gz in steps. The backup writes the dump as many small
	 * gzip "members" (one or more per backup step), so we can stop between two
	 * members and continue in the next request without keeping zlib state.
	 */
	private static function phase_gunzip( array $job, $deadline ) {
		$gz_path  = self::dir( $job['id'], 'incoming/database.sql.gz' );
		$sql_path = self::dir( $job['id'], 'database.sql' );

		if ( ! function_exists( 'inflate_init' ) || ! function_exists( 'inflate_get_read_len' ) ) {
			return self::gunzip_all_at_once( $job, $gz_path, $sql_path );
		}

		$in_pos  = isset( $job['state']['gz_in'] ) ? (int) $job['state']['gz_in'] : 0;
		$out_pos = isset( $job['state']['gz_out'] ) ? (int) $job['state']['gz_out'] : 0;
		$total   = filesize( $gz_path );

		$src = fopen( $gz_path, 'rb' );
		$dst = fopen( $sql_path, 'c' );
		if ( ! $src || ! $dst ) {
			throw new Exception( 'Kunde inte packa upp databasfilen.' );
		}
		// Throw away anything a killed request wrote after the last finished member.
		ftruncate( $dst, $out_pos );
		fseek( $dst, $out_pos );

		while ( $in_pos < $total && microtime( true ) < $deadline ) {
			fseek( $src, $in_pos );
			$ctx  = inflate_init( ZLIB_ENCODING_GZIP );
			$done = false;
			while ( ! feof( $src ) ) {
				$chunk = fread( $src, 1048576 );
				if ( '' === $chunk || false === $chunk ) {
					break;
				}
				$out = @inflate_add( $ctx, $chunk, ZLIB_SYNC_FLUSH ); // phpcs:ignore
				if ( false === $out ) {
					throw new Exception( 'Databasfilen är skadad.' );
				}
				fwrite( $dst, $out );
				if ( ZLIB_STREAM_END === inflate_get_status( $ctx ) ) {
					$done = true;
					break;
				}
			}
			if ( ! $done ) {
				throw new Exception( 'Databasfilen tog slut för tidigt – den är ofullständig.' );
			}
			$in_pos += inflate_get_read_len( $ctx );
			$out_pos = ftell( $dst );
			$job['state']['gz_in']  = $in_pos;
			$job['state']['gz_out'] = $out_pos;
		}
		fclose( $src );
		fclose( $dst );

		$job['progress'] = array(
			'text'    => 'Packar upp databasen',
			'percent' => $total ? round( 100 * $in_pos / $total ) : 100,
		);
		if ( $in_pos >= $total ) {
			return self::gunzip_done( $job, $gz_path );
		}
		return $job;
	}

	private static function gunzip_all_at_once( array $job, $gz_path, $sql_path ) {
		$src = gzopen( $gz_path, 'rb' );
		$dst = fopen( $sql_path, 'wb' );
		if ( ! $src || ! $dst ) {
			throw new Exception( 'Kunde inte packa upp databasfilen.' );
		}
		while ( ! gzeof( $src ) ) {
			$buf = gzread( $src, 1048576 );
			if ( false === $buf ) {
				throw new Exception( 'Databasfilen är skadad.' );
			}
			fwrite( $dst, $buf );
		}
		gzclose( $src );
		fclose( $dst );
		return self::gunzip_done( $job, $gz_path );
	}

	private static function gunzip_done( array $job, $gz_path ) {
		@unlink( $gz_path ); // phpcs:ignore
		self::drop_tables( $job['tmp_prefix'] );
		$job['state']['import_offset'] = 0;
		$job['state']['statements']    = 0;
		$job['phase']                  = 'import';
		return $job;
	}

	private static function phase_import( array $job, $deadline ) {
		global $wpdb;

		$file = self::dir( $job['id'], 'database.sql' );
		$size = filesize( $file );
		$fh   = fopen( $file, 'rb' );
		fseek( $fh, (int) $job['state']['import_offset'] );

		$wpdb->query( 'SET FOREIGN_KEY_CHECKS = 0' );
		$wpdb->query( "SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO'" );
		$suppress = $wpdb->suppress_errors( true );

		$src = $job['manifest']['prefix'];
		$tmp = $job['tmp_prefix'];
		$buf = '';

		while ( ! feof( $fh ) && microtime( true ) < $deadline ) {
			$line = fgets( $fh );
			if ( false === $line ) {
				break;
			}
			if ( '' === $buf ) {
				$trim = ltrim( $line );
				if ( '' === trim( $line ) || 0 === strpos( $trim, '--' ) || 0 === stripos( $trim, 'SET ' ) ) {
					$job['state']['import_offset'] = ftell( $fh );
					continue;
				}
			}
			$buf .= $line;
			if ( strlen( $buf ) > self::IMPORT_MAX_STATEMENT ) {
				throw new Exception( 'En databasrad är orimligt stor – avbryter.' );
			}
			if ( ';' !== substr( rtrim( $line ), -1 ) ) {
				continue;
			}

			$sql = self::rewrite_statement( rtrim( $buf ), $src, $tmp );
			$buf = '';
			if ( null !== $sql ) {
				// wpdb would otherwise reject rows with bytes invalid for the column charset
				// (common in old data) and spend a long time scanning big statements.
				$wpdb->check_current_query = false;
				if ( false === $wpdb->query( $sql ) ) {
					$err = $wpdb->last_error;
					$wpdb->suppress_errors( $suppress );
					throw new Exception( 'Databasimporten misslyckades: ' . $err );
				}
				$job['state']['statements']++;
			}
			$job['state']['import_offset'] = ftell( $fh );
		}
		$wpdb->suppress_errors( $suppress );
		fclose( $fh );

		$job['progress'] = array(
			'text'    => 'Importerar databasen',
			'percent' => $size ? round( 100 * $job['state']['import_offset'] / $size ) : 100,
		);

		if ( $job['state']['import_offset'] >= $size ) {
			@unlink( $file ); // phpcs:ignore
			$job['phase']           = 'rewrite';
			$job['state']['tables'] = self::list_tables( $tmp );
			$job['state']['ti']     = 0;
			$job['state']['last']   = null;
			$job['state']['changed'] = 0;
		}
		return $job;
	}

	/**
	 * Point every table name in a dump statement at the temporary prefix.
	 */
	private static function rewrite_statement( $sql, $src, $tmp ) {
		$q = preg_quote( $src, '/' );
		if ( preg_match( '/^(DROP TABLE IF EXISTS|CREATE TABLE|INSERT INTO)\s+`' . $q . '/i', $sql ) ) {
			$sql = preg_replace( '/^(DROP TABLE IF EXISTS|CREATE TABLE|INSERT INTO)\s+`' . $q . '/i', '$1 `' . $tmp, $sql, 1 );
			if ( 0 === stripos( $sql, 'INSERT INTO' ) ) {
				// REPLACE makes a re-run of the same statement harmless (if a step was killed and retried).
				$sql = 'REPLACE INTO' . substr( $sql, 11 );
			}
			if ( 0 === stripos( $sql, 'CREATE TABLE' ) ) {
				// MySQL 8 collations do not exist in MariaDB – use one that exists in both.
				$sql = preg_replace( '/utf8mb4_0900_[a-z_]+/i', 'utf8mb4_unicode_520_ci', $sql );
				$sql = preg_replace( '/REFERENCES\s+`' . $q . '/i', 'REFERENCES `' . $tmp, $sql );
				// Constraint names are global per database; make them unique.
				$sql = preg_replace( '/CONSTRAINT\s+`([^`]+)`/i', 'CONSTRAINT `$1_' . substr( $tmp, 5, 4 ) . '`', $sql );
			}
			return $sql;
		}
		return null; // Anything else in the dump is ignored.
	}

	private static function list_tables( $prefix ) {
		global $wpdb;
		$rows = $wpdb->get_col( $wpdb->prepare( 'SHOW TABLES LIKE %s', $wpdb->esc_like( $prefix ) . '%' ) );
		return array_values( (array) $rows );
	}

	private static function drop_tables( $prefix ) {
		global $wpdb;
		if ( ! $prefix || 0 !== strpos( $prefix, 'sv' ) ) {
			return;
		}
		$wpdb->query( 'SET FOREIGN_KEY_CHECKS = 0' );
		foreach ( self::list_tables( $prefix ) as $t ) {
			$wpdb->query( 'DROP TABLE IF EXISTS `' . str_replace( '`', '``', $t ) . '`' );
		}
	}

	/**
	 * Search/replace pairs for a move. Full URLs (both http and https) are
	 * replaced with the new site's address including its scheme, then any
	 * scheme-less //old references. Also JSON-escaped variants (Elementor).
	 */
	private static function replace_pairs( array $job ) {
		if ( 'migrate' !== $job['mode'] ) {
			return array();
		}
		$target = isset( $job['target'] ) ? $job['target'] : array( 'home' => home_url(), 'siteurl' => site_url() );
		$map    = array();
		foreach ( array( array( $job['manifest']['site'], $target['home'] ), array( $job['manifest']['siteurl'], $target['siteurl'] ) ) as $pair ) {
			$from = self::normalize_url( $pair[0] );          // //old.se/sub
			$to   = untrailingslashit( $pair[1] );             // https://new.se
			$same_address = ( $from === self::normalize_url( $to ) );
			$same_scheme  = ( strtolower( (string) wp_parse_url( $pair[0], PHP_URL_SCHEME ) ) === strtolower( (string) wp_parse_url( $to, PHP_URL_SCHEME ) ) );
			if ( '' === $from || ( $same_address && $same_scheme ) ) {
				continue;
			}
			$map[ 'https:' . $from ] = $to;
			$map[ 'http:' . $from ]  = $to;
			$map[ $from ]            = self::normalize_url( $to );
		}
		if ( $job['manifest']['abspath'] && untrailingslashit( $job['manifest']['abspath'] ) !== untrailingslashit( ABSPATH ) ) {
			$map[ untrailingslashit( $job['manifest']['abspath'] ) ] = untrailingslashit( ABSPATH );
		}
		$pairs = array();
		foreach ( $map as $from => $to ) {
			if ( '' === $from || $from === $to ) {
				continue;
			}
			$pairs[ $from ]                            = $to;
			$pairs[ str_replace( '/', '\\/', $from ) ] = str_replace( '/', '\\/', $to );
		}
		uksort( $pairs, function ( $a, $b ) {
			return strlen( $b ) - strlen( $a );
		} );
		return $pairs;
	}

	private static function phase_rewrite( array $job, $deadline ) {
		global $wpdb;

		$pairs  = self::replace_pairs( $job );
		$tables = $job['state']['tables'];

		if ( ! $pairs ) {
			$job['phase'] = 'prefix';
			return $job;
		}

		$from = array_keys( $pairs );
		$to   = array_values( $pairs );

		while ( $job['state']['ti'] < count( $tables ) && microtime( true ) < $deadline ) {
			$t       = $tables[ $job['state']['ti'] ];
			$qt      = '`' . str_replace( '`', '``', $t ) . '`';
			$columns = $wpdb->get_results( "SHOW COLUMNS FROM {$qt}", ARRAY_A );
			$text    = array();
			$pk      = array();
			foreach ( (array) $columns as $c ) {
				if ( preg_match( '/char|text|json|enum|set/i', $c['Type'] ) ) {
					$text[] = $c['Field'];
				}
				if ( 'PRI' === $c['Key'] ) {
					$pk[] = $c['Field'];
				}
			}
			if ( ! $text || 1 !== count( $pk ) ) {
				// No text columns, or no single-column primary key: handle in one go below if needed.
				if ( $text && $pk ) {
					$job = self::rewrite_whole_table( $job, $t, $text, $pk, $from, $to );
				}
				$job['state']['ti']++;
				$job['state']['last'] = null;
				continue;
			}

			$key   = $pk[0];
			$where = array();
			foreach ( $text as $col ) {
				foreach ( $from as $f ) {
					$where[] = $wpdb->prepare( '`' . $col . '` LIKE %s', '%' . $wpdb->esc_like( $f ) . '%' );
				}
			}
			$cond = '(' . implode( ' OR ', $where ) . ')';
			if ( null !== $job['state']['last'] ) {
				$cond .= $wpdb->prepare( " AND `{$key}` > %s", $job['state']['last'] );
			}
			$sel  = '`' . $key . '`, `' . implode( '`, `', array_diff( $text, array( $key ) ) ) . '`';
			$rows = $wpdb->get_results( "SELECT {$sel} FROM {$qt} WHERE {$cond} ORDER BY `{$key}` LIMIT 200", ARRAY_A );

			foreach ( (array) $rows as $row ) {
				$set = array();
				foreach ( $text as $col ) {
					if ( $col === $key || null === $row[ $col ] ) {
						continue;
					}
					$new = self::replace_value( $row[ $col ], $from, $to );
					if ( $new !== $row[ $col ] ) {
						$set[ $col ] = $new;
					}
				}
				if ( $set ) {
					$wpdb->update( $t, $set, array( $key => $row[ $key ] ) );
					$job['state']['changed']++;
				}
				$job['state']['last'] = $row[ $key ];
			}

			if ( count( (array) $rows ) < 200 ) {
				$job['state']['ti']++;
				$job['state']['last'] = null;
			}
		}

		$job['progress'] = array(
			'text'    => 'Byter adresser i databasen',
			'percent' => count( $tables ) ? round( 100 * $job['state']['ti'] / count( $tables ) ) : 100,
		);
		if ( $job['state']['ti'] >= count( $tables ) ) {
			$job['phase'] = 'prefix';
		}
		return $job;
	}

	private static function rewrite_whole_table( array $job, $t, $text, $pk, $from, $to ) {
		global $wpdb;
		$qt   = '`' . str_replace( '`', '``', $t ) . '`';
		$rows = $wpdb->get_results( "SELECT * FROM {$qt}", ARRAY_A );
		foreach ( (array) $rows as $row ) {
			$set = array();
			foreach ( $text as $col ) {
				if ( in_array( $col, $pk, true ) || null === $row[ $col ] ) {
					continue;
				}
				$new = self::replace_value( $row[ $col ], $from, $to );
				if ( $new !== $row[ $col ] ) {
					$set[ $col ] = $new;
				}
			}
			if ( $set ) {
				$where = array();
				foreach ( $pk as $k ) {
					$where[ $k ] = $row[ $k ];
				}
				$wpdb->update( $t, $set, $where );
				$job['state']['changed']++;
			}
		}
		return $job;
	}

	/**
	 * Replace in a value without breaking PHP-serialized data.
	 * Serialized strings are rewritten token by token (lengths fixed),
	 * so objects are never instantiated.
	 */
	public static function replace_value( $value, array $from, array $to ) {
		$hit = false;
		foreach ( $from as $f ) {
			if ( false !== strpos( $value, $f ) ) {
				$hit = true;
				break;
			}
		}
		if ( ! $hit ) {
			return $value;
		}
		if ( is_serialized( $value, false ) ) {
			$ok  = true;
			$new = self::replace_serialized( $value, $from, $to, $ok );
			if ( $ok && false !== @unserialize( $new, array( 'allowed_classes' => false ) ) ) { // phpcs:ignore
				return $new;
			}
			return $value; // Could not parse safely – leave untouched rather than corrupt it.
		}
		return str_replace( $from, $to, $value );
	}

	private static function replace_serialized( $s, array $from, array $to, &$ok ) {
		$out = '';
		$len = strlen( $s );
		$i   = 0;
		while ( $i < $len ) {
			$c = $s[ $i ];
			if ( 's' === $c && $i + 1 < $len && ':' === $s[ $i + 1 ] && ( 0 === $i || false !== strpos( '{;}', $s[ $i - 1 ] ) ) ) {
				$j = $i + 2;
				while ( $j < $len && ctype_digit( $s[ $j ] ) ) {
					$j++;
				}
				$n = (int) substr( $s, $i + 2, $j - $i - 2 );
				if ( ':"' !== substr( $s, $j, 2 ) || '";' !== substr( $s, $j + 2 + $n, 2 ) ) {
					$ok = false;
					return $s;
				}
				$inner = substr( $s, $j + 2, $n );
				$new   = self::replace_value( $inner, $from, $to );
				$out  .= 's:' . strlen( $new ) . ':"' . $new . '";';
				$i     = $j + 2 + $n + 2;
				continue;
			}
			$out .= $c;
			$i++;
		}
		return $out;
	}

	/** Keys in options/usermeta that carry the table prefix (roles, capabilities). */
	private static function phase_prefix( array $job, $deadline ) {
		global $wpdb;
		$src = $job['manifest']['prefix'];
		$dst = $wpdb->base_prefix;
		$tmp = $job['tmp_prefix'];

		if ( $src !== $dst ) {
			$len = strlen( $src );
			// In options only the roles carry the table prefix. Options that merely start
			// with "wp_" (wp_page_for_privacy_policy, wp_mail_smtp …) must keep their names.
			$wpdb->query(
				$wpdb->prepare(
					"UPDATE `{$tmp}options` SET option_name = %s WHERE option_name = %s",
					$dst . 'user_roles',
					$src . 'user_roles'
				)
			);
			// User options (capabilities, user_level, settings …) are stored with the prefix.
			$wpdb->query(
				$wpdb->prepare(
					"UPDATE `{$tmp}usermeta` SET meta_key = CONCAT(%s, SUBSTRING(meta_key, %d)) WHERE meta_key LIKE %s",
					$dst,
					$len + 1,
					$wpdb->esc_like( $src ) . '%'
				)
			);
		}

		if ( 'migrate' === $job['mode'] && ! empty( $job['target'] ) ) {
			// The new address, with the new site's scheme (http/https), whatever the search/replace did.
			foreach ( array( 'home', 'siteurl' ) as $opt ) {
				$wpdb->query( $wpdb->prepare( "UPDATE `{$tmp}options` SET option_value = %s WHERE option_name = %s", $job['target'][ $opt ], $opt ) );
			}
		}

		$job['phase']             = 'extract';
		$job['state']['db_ready'] = true;
		return $job;
	}

	/* -- 3. Files: unpack into the staging folder ----------------------- */

	private static function should_skip_entry( $name, $mode, array $sajtvakt_folders = array() ) {
		if ( '' === $name || false !== strpos( $name, '..' ) || '/' === $name[0] || false !== strpos( $name, ':' ) || false !== strpos( $name, '\\' ) ) {
			return true;
		}
		if ( 0 === strpos( $name, 'root/' ) ) {
			// Only these files from the WordPress root are ever written back.
			// wp-config.php never is: this site keeps its own database settings.
			$allowed = ( 'migrate' === $mode ) ? array( 'root/robots.txt' ) : array( 'root/.htaccess', 'root/robots.txt', 'root/.user.ini' );
			return ! in_array( $name, $allowed, true );
		}
		if ( 0 !== strpos( $name, 'wp-content/' ) ) {
			return true;
		}
		if ( 0 === strpos( $name, 'wp-content/sajtvakt-data/' ) ) {
			return true;
		}
		// Any copy of Sajtvakt in the backup is skipped – the running copy stays (whatever its folder is called).
		$folders = array_merge( array( basename( SAJTVAKT_DIR ) ), $sajtvakt_folders );
		foreach ( $folders as $folder ) {
			if ( 0 === strpos( $name, 'wp-content/plugins/' . $folder . '/' ) ) {
				return true;
			}
		}
		if ( 'migrate' === $mode && in_array( $name, self::$host_specific, true ) ) {
			return true;
		}
		return false;
	}

	/** Plugin folders in the backup that contain Sajtvakt (it may have another folder name there). */
	private static function find_sajtvakt_folders( array $parts, $id ) {
		$found = array();
		foreach ( $parts as $part ) {
			$zip = new ZipArchive();
			if ( true !== $zip->open( self::dir( $id, 'incoming/' . $part ) ) ) {
				continue;
			}
			for ( $i = 0; $i < $zip->numFiles; $i++ ) {
				$name = $zip->getNameIndex( $i );
				if ( preg_match( '#^wp-content/plugins/([^/]+)/rescue\.php$#', (string) $name, $m ) ) {
					$main = $zip->getFromName( 'wp-content/plugins/' . $m[1] . '/sajtvakt.php' );
					if ( false !== $main && preg_match( '/Plugin Name:\s*Sajtvakt\s*$/m', $main ) ) {
						$found[] = $m[1];
					}
				}
			}
			$zip->close();
		}
		return array_values( array_unique( $found ) );
	}

	private static function phase_extract( array $job, $deadline ) {
		$parts = array();
		foreach ( array_keys( $job['files'] ) as $name ) {
			if ( 0 === strpos( $name, 'files-' ) ) {
				$parts[] = $name;
			}
		}
		sort( $parts );

		if ( ! isset( $job['state']['part'] ) ) {
			$job['state']['part']       = 0;
			$job['state']['entry']      = 0;
			$job['state']['files']      = 0;
			$job['state']['sv_folders'] = self::find_sajtvakt_folders( $parts, $job['id'] );
		}
		$sv_folders = (array) $job['state']['sv_folders'];
		$target = self::dir( $job['id'], 'extract' );

		while ( $job['state']['part'] < count( $parts ) && microtime( true ) < $deadline ) {
			$path = self::dir( $job['id'], 'incoming/' . $parts[ $job['state']['part'] ] );
			$zip  = new ZipArchive();
			if ( true !== $zip->open( $path ) ) {
				throw new Exception( 'Kunde inte öppna ' . $parts[ $job['state']['part'] ] . '.' );
			}
			$n = $zip->numFiles;
			while ( $job['state']['entry'] < $n && microtime( true ) < $deadline ) {
				$batch = array();
				for ( $k = 0; $k < 200 && $job['state']['entry'] < $n; $k++ ) {
					$name = $zip->getNameIndex( $job['state']['entry'] );
					$job['state']['entry']++;
					if ( false === $name || '/' === substr( $name, -1 ) || self::should_skip_entry( $name, $job['mode'], $sv_folders ) ) {
						continue;
					}
					$batch[] = $name;
				}
				if ( $batch && ! $zip->extractTo( $target, $batch ) ) {
					throw new Exception( 'Kunde inte packa upp filer (fullt på disken?).' );
				}
				$job['state']['files'] += count( $batch );
			}
			$zip->close();
			if ( $job['state']['entry'] >= $n ) {
				@unlink( $path ); // phpcs:ignore
				$job['state']['part']++;
				$job['state']['entry'] = 0;
			}
		}

		$job['progress'] = array(
			'text'    => 'Packar upp filer',
			'percent' => count( $parts ) ? round( 100 * $job['state']['part'] / count( $parts ) ) : 100,
			'files'   => $job['state']['files'],
		);
		if ( $job['state']['part'] >= count( $parts ) ) {
			$job['phase'] = 'commit';
		}
		return $job;
	}

	/* -- 4. Commit: switch everything in -------------------------------- */

	/** Set when a failed commit could not be fully undone – then nothing may be cleaned up automatically. */
	private static $undo_failed = false;

	/**
	 * Switch the restored site in. Database first (one atomic RENAME, old tables
	 * kept), then the code folders. Every step is journaled; if anything fails,
	 * everything already switched is switched back and the live site is as before.
	 */
	private static function phase_commit( array $job, $deadline ) {
		global $wpdb;

		@set_time_limit( 300 ); // phpcs:ignore
		$extract    = self::dir( $job['id'], 'extract' );
		$trash      = Sajtvakt_Core::data_dir( 'trash/restore-' . $job['id'] );
		$own_folder = basename( SAJTVAKT_DIR );
		$tmp        = $job['tmp_prefix'];
		$dst        = $wpdb->base_prefix;
		$old        = 'svold' . substr( $tmp, 5 );
		@mkdir( $trash, 0755, true ); // phpcs:ignore

		$swaps = array(
			'plugins' => WP_PLUGIN_DIR,
			'themes'  => get_theme_root(),
		);
		if ( 'restore' === $job['mode'] ) {
			// Same site: put back exactly what was there.
			$swaps['mu-plugins'] = WPMU_PLUGIN_DIR;
			$swaps['languages']  = WP_LANG_DIR;
		}
		// On a move, mu-plugins and languages are merged instead (in the merge step),
		// so the new host's own must-use plugins stay alongside the site's.

		$journal = array();
		$undo_db = null;
		try {
			// a) Database: one atomic RENAME of all tables. The old ones are kept until the very end.
			if ( ! empty( $job['state']['db_ready'] ) ) {
				self::drop_tables( $old );
				$existing = self::list_tables( $dst );
				$forward  = array();
				$back     = array();
				foreach ( self::list_tables( $tmp ) as $t ) {
					$suffix = substr( $t, strlen( $tmp ) );
					$live   = $dst . $suffix;
					if ( in_array( $live, $existing, true ) ) {
						$forward[] = "`{$live}` TO `{$old}{$suffix}`";
					}
					$forward[] = "`{$t}` TO `{$live}`";
					$back[]    = "`{$live}` TO `{$t}`";
					if ( in_array( $live, $existing, true ) ) {
						$back[] = "`{$old}{$suffix}` TO `{$live}`";
					}
				}
				if ( $forward ) {
					if ( false === $wpdb->query( 'RENAME TABLE ' . implode( ', ', $forward ) ) ) {
						throw new Exception( 'Kunde inte byta in den återställda databasen: ' . $wpdb->last_error );
					}
					$undo_db = 'RENAME TABLE ' . implode( ', ', $back );
				}
			}

			// b) Code folders: swapped whole, so nothing from the old install lingers.
			foreach ( $swaps as $sub => $live ) {
				$staged = $extract . '/wp-content/' . $sub;
				if ( ! is_dir( $staged ) ) {
					continue;
				}
				$parked = $trash . '/' . $sub;
				if ( file_exists( $live ) && ! @rename( $live, $parked ) ) { // phpcs:ignore
					throw new Exception( "Kunde inte flytta undan mappen {$sub} (rättigheter?)." );
				}
				if ( ! @rename( $staged, $live ) ) { // phpcs:ignore
					@rename( $parked, $live ); // phpcs:ignore
					throw new Exception( "Kunde inte flytta in mappen {$sub}." );
				}
				$journal[] = array( 'sub' => $sub, 'live' => $live, 'parked' => $parked, 'staged' => $staged );
				if ( 'plugins' === $sub && is_dir( $parked . '/' . $own_folder ) ) {
					// Keep the running copy of Sajtvakt.
					if ( ! @rename( $parked . '/' . $own_folder, $live . '/' . $own_folder ) // phpcs:ignore
						&& ! Sajtvakt_Core::rcopy( $parked . '/' . $own_folder, $live . '/' . $own_folder ) ) {
						throw new Exception( 'Kunde inte behålla Sajtvakt-tillägget i den nya tilläggsmappen.' );
					}
				}
			}
		} catch ( Throwable $e ) {
			$undone = true;
			foreach ( array_reverse( $journal ) as $j ) {
				if ( 'plugins' === $j['sub'] && is_dir( $j['live'] . '/' . $own_folder ) && ! is_dir( $j['parked'] . '/' . $own_folder ) ) {
					@rename( $j['live'] . '/' . $own_folder, $j['parked'] . '/' . $own_folder ); // phpcs:ignore
				}
				$undone = @rename( $j['live'], $j['staged'] ) && @rename( $j['parked'], $j['live'] ) && $undone; // phpcs:ignore
			}
			if ( $undo_db ) {
				$undone = ( false !== $wpdb->query( $undo_db ) ) && $undone;
			}
			wp_cache_flush();
			if ( ! $undone ) {
				self::$undo_failed = true;
				throw new Exception( 'Inbytet misslyckades och kunde inte backas helt: ' . $e->getMessage() . ' Kontrollera sajten och återställ vid behov från backupen.' );
			}
			throw new Exception( 'Inbytet misslyckades, men sajten är som förut: ' . $e->getMessage() );
		}

		// c) Committed. Tidy up what the restored database needs.
		if ( $undo_db ) {
			wp_cache_flush();
			self::keep_self_active( isset( $job['state']['sv_folders'] ) ? (array) $job['state']['sv_folders'] : array() );
			if ( 'migrate' === $job['mode'] ) {
				delete_option( 'sajtvakt_last_backup' );
			}
			delete_option( 'rewrite_rules' );
		}
		self::purge_caches( 'migrate' === $job['mode'] );
		$job['state']['old_prefix'] = $old;

		// d) Everything else (uploads, loose files) is merged in, file by file, in the next steps.
		$list = self::dir( $job['id'], 'merge.php' );
		$out  = fopen( $list, 'wb' );
		fwrite( $out, Sajtvakt_Core::GUARD );
		$count = 0;
		if ( is_dir( $extract ) ) {
			$it = new RecursiveIteratorIterator( new RecursiveDirectoryIterator( $extract, FilesystemIterator::SKIP_DOTS | FilesystemIterator::UNIX_PATHS ) );
			foreach ( $it as $file ) {
				if ( $file->isFile() ) {
					fwrite( $out, substr( wp_normalize_path( $file->getPathname() ), strlen( wp_normalize_path( $extract ) ) + 1 ) . "\n" );
					$count++;
				}
			}
		}
		fclose( $out );

		$job['state']['merge_total'] = $count;
		$job['state']['merge_index'] = 0;
		$job['phase']                = 'merge';
		$job['progress']             = array( 'text' => 'Byter in den återställda sajten' );
		return $job;
	}

	private static function keep_self_active( array $backup_copies = array() ) {
		$self   = plugin_basename( SAJTVAKT_FILE );
		$active = (array) get_option( 'active_plugins', array() );
		// Another folder name for Sajtvakt in the backup must not stay active (it was not restored).
		$active = array_values( array_filter( $active, function ( $p ) use ( $backup_copies, $self ) {
			return $p === $self || ! in_array( dirname( $p ), $backup_copies, true );
		} ) );
		if ( ! in_array( $self, $active, true ) ) {
			$active[] = $self;
		}
		sort( $active );
		update_option( 'active_plugins', $active );
	}

	/** Page caches and generated CSS from before the switch would show the old site. */
	private static function purge_caches( $moved ) {
		foreach ( array( 'cache', 'et-cache' ) as $sub ) {
			$dir = WP_CONTENT_DIR . '/' . $sub;
			if ( is_dir( $dir ) ) {
				foreach ( (array) scandir( $dir ) as $entry ) {
					if ( '.' !== $entry && '..' !== $entry && 'index.php' !== $entry ) {
						Sajtvakt_Core::rrmdir( $dir . '/' . $entry );
					}
				}
			}
		}
		if ( $moved ) {
			// Elementor's generated CSS contains the old address; it is rebuilt on the next visit.
			$uploads = wp_upload_dir( null, false );
			Sajtvakt_Core::rrmdir( $uploads['basedir'] . '/elementor/css' );
			delete_post_meta_by_key( '_elementor_css' );
			delete_option( '_elementor_global_css' );
		}
		do_action( 'litespeed_purge_all' );
		wp_cache_flush();
	}

	private static function phase_merge( array $job, $deadline ) {
		$extract = self::dir( $job['id'], 'extract' );
		$lines   = file( self::dir( $job['id'], 'merge.php' ), FILE_IGNORE_NEW_LINES );
		array_shift( $lines );
		$total = count( $lines );
		$i     = (int) $job['state']['merge_index'];

		$content = untrailingslashit( wp_normalize_path( WP_CONTENT_DIR ) );
		$root    = untrailingslashit( wp_normalize_path( ABSPATH ) );

		while ( $i < $total && microtime( true ) < $deadline ) {
			$rel = $lines[ $i ];
			$i++;
			if ( 'migrate' === $job['mode'] && false !== strpos( $rel, '/uploads/elementor/css/' ) ) {
				continue; // Generated for the old address – Elementor rebuilds it.
			}
			if ( 0 === strpos( $rel, 'wp-content/' ) ) {
				$dest = $content . '/' . substr( $rel, 11 );
			} elseif ( 0 === strpos( $rel, 'root/' ) ) {
				$dest = $root . '/' . substr( $rel, 5 );
			} else {
				continue;
			}
			$dir = dirname( $dest );
			if ( ! is_dir( $dir ) ) {
				@mkdir( $dir, 0755, true ); // phpcs:ignore
			}
			if ( ! @rename( $extract . '/' . $rel, $dest ) ) { // phpcs:ignore
				@copy( $extract . '/' . $rel, $dest ); // phpcs:ignore
			}
		}

		$job['state']['merge_index'] = $i;
		$job['progress']             = array(
			'text'    => 'Lägger på plats uppladdade filer',
			'percent' => $total ? round( 100 * $i / $total ) : 100,
		);
		if ( $i >= $total ) {
			$job['phase'] = 'finalize';
		}
		return $job;
	}

	private static function phase_finalize( array $job, $deadline ) {
		if ( ! empty( $job['state']['old_prefix'] ) ) {
			self::drop_tables( $job['state']['old_prefix'] );
		}
		Sajtvakt_Core::rrmdir( Sajtvakt_Core::data_dir( 'trash/restore-' . $job['id'] ) );
		Sajtvakt_Core::rrmdir( self::dir( $job['id'], 'extract' ) );
		Sajtvakt_Core::rrmdir( self::dir( $job['id'], 'incoming' ) );
		@unlink( self::dir( $job['id'], 'merge.php' ) ); // phpcs:ignore
		Sajtvakt_Core::clear_maintenance();
		wp_cache_flush();
		if ( function_exists( 'opcache_reset' ) ) {
			@opcache_reset(); // phpcs:ignore
		}

		$result = array(
			'mode'    => $job['mode'],
			'from'    => $job['manifest']['site'],
			'home'    => get_option( 'home' ),
			'changed' => isset( $job['state']['changed'] ) ? $job['state']['changed'] : 0,
		);
		update_option( 'sajtvakt_last_restore', array_merge( $result, array( 'time' => time() ) ), false );

		$job['result']   = $result;
		$job['status']   = 'done';
		$job['phase']    = 'done';
		$job['progress'] = array( 'text' => 'Klart', 'percent' => 100 );
		return $job;
	}

	/* ------------------------------------------------------------------ */

	public static function cancel( $id ) {
		$job = self::load( $id );
		if ( ! $job ) {
			return array( 'ok' => true );
		}
		// Never pull the rug from under a step that is still running.
		$lock = fopen( self::dir( $id, 'job.lock' ), 'c' );
		if ( ! $lock || ! flock( $lock, LOCK_EX | LOCK_NB ) ) {
			return array( 'ok' => false, 'error' => 'busy', 'message' => 'Ett steg körs fortfarande – försök igen om en stund.' );
		}
		$job = self::load( $id );
		// A commit that failed and was fully undone is safe to clean up; anything switched in is not.
		$undone_commit = ( 'commit' === $job['phase'] && 'error' === $job['status'] && empty( $job['state']['undo_failed'] ) );
		if ( ! $undone_commit && in_array( $job['phase'], array( 'commit', 'merge', 'finalize', 'done' ), true ) ) {
			flock( $lock, LOCK_UN );
			fclose( $lock );
			return array( 'ok' => false, 'error' => 'too_late', 'message' => 'Återställningen byts in eller är inbytt och kan inte avbrytas.' );
		}
		self::drop_tables( $job['tmp_prefix'] );
		flock( $lock, LOCK_UN );
		fclose( $lock );
		Sajtvakt_Core::rrmdir( self::dir( $id ) );
		return array( 'ok' => true );
	}

	public static function status( $id ) {
		$job = self::load( $id );
		if ( ! $job ) {
			Sajtvakt_Core::send_error( 'no_job', 'Återställningsjobbet finns inte.', 404 );
		}
		return self::view( $job );
	}
}
