<?php
defined( 'ABSPATH' ) || exit;

/**
 * Resumable backups in small steps, so they work on shared hosting with short
 * execution time limits. The server drives the job: start → step … step → download → finish.
 *
 * Output per job:
 *   database.sql.gz   – full dump of the site's tables (prefix-filtered)
 *   files-001.zip …   – wp-content plus wp-config.php/.htaccess, split in parts
 *   index.json.gz     – (incremental only) every file on the site: [path, size, mtime]
 *   manifest.json     – what the backup contains, with sizes and SHA-256
 *
 * Incremental: the server passes `base` = the job id of the last backup it stored.
 * If that matches our saved baseline, only new or changed files (size or mtime)
 * are zipped; the server takes the rest from the backup it already has. The
 * baseline is only replaced when the server confirms it stored the new backup
 * (finish with keep=true), so a failed transfer never leaves us out of step.
 */
final class Sajtvakt_Backup {

	const ROWS_PER_QUERY   = 500;
	const ROWS_PER_INSERT  = 100;
	const INSERT_MAX_BYTES = 1048576;     // ~1 MB per INSERT statement.
	const SELECT_MAX_BYTES = 4194304;     // Aim for ~4 MB of rows per SELECT (memory).
	const PART_MAX_BYTES   = 64 * 1024 * 1024; // Raw bytes added per zip part (keeps each step short).
	const PART_MAX_FILES   = 3000;
	const DOWNLOAD_MAX     = 16 * 1024 * 1024;

	/** Folders directly under wp-content that are caches or other backup tools. */
	private static $skip_dirs = array(
		'sajtvakt-data', 'cache', 'upgrade', 'upgrade-temp-backup', 'updraft', 'ai1wm-backups',
		'backups-dup-lite', 'backups-dup-pro', 'wpvividbackups', 'backup-guard', 'et-cache',
		'litespeed', 'wflogs', 'node_modules', '.git',
	);

	/** Already-compressed formats are stored instead of deflated (much faster). */
	private static $store_ext = array(
		'jpg', 'jpeg', 'png', 'gif', 'webp', 'avif', 'heic', 'mp4', 'm4v', 'mov', 'webm', 'mp3', 'm4a',
		'ogg', 'zip', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar', 'pdf', 'woff', 'woff2', 'docx', 'xlsx', 'pptx',
	);

	/* ------------------------------------------------------------------ */

	private static function job_file( $id ) {
		return Sajtvakt_Core::data_dir( 'jobs/' . $id . '.php' );
	}

	private static function job_dir( $id ) {
		return Sajtvakt_Core::data_dir( 'backups/' . $id );
	}

	private static function load( $id ) {
		if ( ! Sajtvakt_Core::valid_id( (string) $id ) ) {
			return null;
		}
		return Sajtvakt_Core::read_json( self::job_file( $id ) );
	}

	private static function save( array $job ) {
		$job['updated'] = time();
		Sajtvakt_Core::write_json( self::job_file( $job['id'] ), $job );
		return $job;
	}

	private static function public_view( array $job ) {
		return array(
			'ok'       => true,
			'job'      => $job['id'],
			'status'   => $job['status'],
			'phase'    => $job['phase'],
			'progress' => array(
				'tables_done'  => $job['db']['ti'],
				'tables_total' => count( $job['db']['tables'] ),
				'files_done'   => $job['files']['index'],
				'files_total'  => $job['files']['total'],
			),
			'outputs'  => $job['outputs'],
			'skipped'  => $job['files']['skipped'],
			'error'    => $job['error'],
		);
	}

	/* ------------------------------------------------------------------ */
	/* Start                                                               */
	/* ------------------------------------------------------------------ */

	public static function start( array $params ) {
		global $wpdb;

		Sajtvakt_Core::ensure_data_dir();
		Sajtvakt_Core::cleanup();

		$include_db    = ! isset( $params['database'] ) || (bool) $params['database'];
		$include_files = ! isset( $params['files'] ) || (bool) $params['files'];
		$max_mb        = isset( $params['max_file_mb'] ) ? max( 1, (int) $params['max_file_mb'] ) : 512;
		$base          = isset( $params['base'] ) && Sajtvakt_Core::valid_id( (string) $params['base'] ) ? (string) $params['base'] : '';

		if ( $include_files && ! class_exists( 'ZipArchive' ) ) {
			Sajtvakt_Core::send_error( 'no_zip', 'PHP-tillägget ZipArchive saknas på webbhotellet, så filer kan inte säkerhetskopieras.', 500 );
		}

		$id = bin2hex( random_bytes( 16 ) );
		if ( ! @mkdir( self::job_dir( $id ), 0755, true ) ) { // phpcs:ignore
			Sajtvakt_Core::send_error( 'not_writable', 'Kunde inte skapa en mapp för backupen i wp-content.', 500 );
		}
		@file_put_contents( self::job_dir( $id ) . '/index.php', "<?php // Tyst.\n" ); // phpcs:ignore

		$tables = array();
		$pks    = array();
		if ( $include_db ) {
			$rows = $wpdb->get_results( $wpdb->prepare( 'SHOW FULL TABLES WHERE Table_type = %s', 'BASE TABLE' ), ARRAY_N );
			foreach ( (array) $rows as $row ) {
				$t = $row[0];
				if ( 0 === strpos( $t, $wpdb->base_prefix ) ) {
					$tables[] = $t;
				}
			}
			$limits = array();
			foreach ( (array) $wpdb->get_results( 'SHOW TABLE STATUS', ARRAY_A ) as $st ) {
				$avg = max( 1, (int) $st['Avg_row_length'] );
				// Elementor rows can be megabytes each – fetch fewer at a time from such tables.
				$limits[ $st['Name'] ] = max( 5, min( self::ROWS_PER_QUERY, (int) floor( self::SELECT_MAX_BYTES / $avg ) ) );
			}
			foreach ( $tables as $t ) {
				$keys = $wpdb->get_results( 'SHOW KEYS FROM ' . self::q( $t ) . " WHERE Key_name = 'PRIMARY'", ARRAY_A );
				$cols = array();
				foreach ( (array) $keys as $k ) {
					$cols[ (int) $k['Seq_in_index'] ] = $k['Column_name'];
				}
				ksort( $cols );
				$pks[ $t ] = array_values( $cols );
			}
		}

		$job = array(
			'id'      => $id,
			'created' => time(),
			'updated' => time(),
			'status'  => 'running',
			'phase'   => $include_db ? 'db' : ( $include_files ? 'scan' : 'finalize' ),
			'options' => array(
				'database'    => $include_db,
				'files'       => $include_files,
				'max_file_mb' => $max_mb,
				'base'        => $base,
			),
			'db'      => array(
				'tables'  => $tables,
				'pk'      => $pks,
				'limit'   => isset( $limits ) ? array_intersect_key( $limits, array_flip( $tables ) ) : array(),
				'ti'      => 0,
				'offset'  => 0,
				'rows'    => 0,
				'started' => false,
				'gz_size' => 0,
			),
			'files'   => array( 'total' => 0, 'index' => 0, 'part' => 0, 'bytes' => 0, 'skipped' => array(), 'failed' => array(), 'all' => 0, 'incremental' => false ),
			'outputs' => array(),
			'error'   => null,
		);
		$job = self::save( $job );
		return self::public_view( $job );
	}

	/* ------------------------------------------------------------------ */
	/* Step                                                                */
	/* ------------------------------------------------------------------ */

	public static function step( $id, $budget ) {
		$job = self::load( $id );
		if ( ! $job ) {
			Sajtvakt_Core::send_error( 'no_job', 'Backupjobbet finns inte (kanske redan städat).', 404 );
		}
		if ( 'running' !== $job['status'] ) {
			return self::public_view( $job );
		}

		$lock = fopen( self::job_file( $id ) . '.lock', 'c' );
		if ( ! $lock || ! flock( $lock, LOCK_EX | LOCK_NB ) ) {
			Sajtvakt_Core::send_error( 'busy', 'Ett steg körs redan för det här jobbet.', 409 );
		}

		$budget   = max( 3, min( 60, (int) $budget ) );
		$deadline = microtime( true ) + $budget;
		@set_time_limit( $budget + 60 ); // phpcs:ignore
		ignore_user_abort( true );

		try {
			while ( 'running' === $job['status'] && microtime( true ) < $deadline ) {
				switch ( $job['phase'] ) {
					case 'db':
						$job = self::step_db( $job, $deadline );
						break;
					case 'scan':
						$job = self::step_scan( $job );
						break;
					case 'files':
						$job = self::step_files( $job, $deadline );
						break;
					case 'finalize':
						$job = self::finalize( $job );
						break;
					default:
						throw new Exception( 'Okänd fas.' );
				}
				$job = self::save( $job );
			}
		} catch ( Throwable $e ) {
			$job['status'] = 'error';
			$job['error']  = $e->getMessage();
			$job           = self::save( $job );
		}

		flock( $lock, LOCK_UN );
		fclose( $lock );
		return self::public_view( $job );
	}

	/* -- Database ------------------------------------------------------ */

	private static function q( $name ) {
		return '`' . str_replace( '`', '``', $name ) . '`';
	}

	private static function esc( $value ) {
		global $wpdb;
		return $wpdb->remove_placeholder_escape( $wpdb->_real_escape( (string) $value ) );
	}

	private static function step_db( array $job, $deadline ) {
		global $wpdb;

		$file = self::job_dir( $job['id'] ) . '/database.sql.gz';

		// If an earlier step was killed half-way, drop what it wrote after the last saved point.
		$committed = isset( $job['db']['gz_size'] ) ? (int) $job['db']['gz_size'] : 0;
		clearstatcache( true, $file );
		if ( file_exists( $file ) && filesize( $file ) > $committed ) {
			$fh = fopen( $file, 'r+' );
			ftruncate( $fh, $committed );
			fclose( $fh );
		}

		$gz = gzopen( $file, 'ab6' );
		if ( ! $gz ) {
			throw new Exception( 'Kunde inte skriva databasfilen.' );
		}

		if ( ! $job['db']['started'] ) {
			$charset = $wpdb->charset ? $wpdb->charset : 'utf8mb4';
			gzwrite( $gz, "-- Sajtvakt " . Sajtvakt_Core::VERSION . " databasexport\n"
				. '-- Webbplats: ' . home_url( '/' ) . "\n"
				. '-- Skapad: ' . gmdate( 'Y-m-d H:i:s' ) . " UTC\n\n"
				. "SET NAMES {$charset};\nSET FOREIGN_KEY_CHECKS = 0;\nSET SQL_MODE = 'NO_AUTO_VALUE_ON_ZERO';\n\n" );
			$job['db']['started'] = true;
		}

		$tables  = $job['db']['tables'];
		$written = 0;
		while ( $job['db']['ti'] < count( $tables ) && microtime( true ) < $deadline ) {
			// Start a new gzip member every ~8 MB so a restore can unpack in steps.
			if ( $written > 8 * 1024 * 1024 ) {
				gzclose( $gz );
				$gz      = gzopen( $file, 'ab6' );
				$written = 0;
			}
			$t      = $tables[ $job['db']['ti'] ];
			$offset = (int) $job['db']['offset'];
			$limit  = isset( $job['db']['limit'][ $t ] ) ? (int) $job['db']['limit'][ $t ] : self::ROWS_PER_QUERY;

			if ( 0 === $offset ) {
				$create = $wpdb->get_row( 'SHOW CREATE TABLE ' . self::q( $t ), ARRAY_N );
				if ( ! $create ) {
					throw new Exception( "Kunde inte läsa strukturen för tabellen {$t}." );
				}
				gzwrite( $gz, "\n-- Tabell {$t}\nDROP TABLE IF EXISTS " . self::q( $t ) . ";\n" . $create[1] . ";\n" );
			}

			$pk         = isset( $job['db']['pk'][ $t ] ) ? $job['db']['pk'][ $t ] : array();
			$order_cols = $pk;
			if ( ! $order_cols ) {
				// Utan primärnyckel är radordningen inte garanterad mellan frågor – sortera på alla
				// kolumner, annars kan rader dubbleras eller tappas när dumpen tas i flera steg.
				if ( ! isset( $job['db']['allcols'][ $t ] ) ) {
					$cols                       = $wpdb->get_col( 'SHOW COLUMNS FROM ' . self::q( $t ), 0 );
					$job['db']['allcols'][ $t ] = $cols ? $cols : array();
				}
				$order_cols = $job['db']['allcols'][ $t ];
			}
			$order = $order_cols ? ' ORDER BY ' . implode( ', ', array_map( array( __CLASS__, 'q' ), $order_cols ) ) : '';
			if ( 1 === count( $pk ) ) {
				// Keyset pagination: fast on big tables, and no skipped rows if the table changes meanwhile.
				$where = ( 0 === $offset || ! isset( $job['db']['last'] ) ) ? '' : $wpdb->prepare( ' WHERE ' . self::q( $pk[0] ) . ' > %s', $job['db']['last'] );
				$sql   = 'SELECT * FROM ' . self::q( $t ) . $where . $order . ' LIMIT ' . $limit;
			} else {
				$sql = 'SELECT * FROM ' . self::q( $t ) . $order . ' LIMIT ' . $offset . ', ' . $limit;
			}
			$rows = $wpdb->get_results( $sql, ARRAY_A );
			if ( '' !== $wpdb->last_error ) {
				throw new Exception( "Databasfel i {$t}: " . $wpdb->last_error );
			}

			if ( $rows ) {
				$head   = 'INSERT INTO ' . self::q( $t ) . ' (' . implode( ', ', array_map( array( __CLASS__, 'q' ), array_keys( $rows[0] ) ) ) . ") VALUES\n";
				$values = array();
				$bytes  = 0;
				foreach ( $rows as $row ) {
					$vals = array();
					foreach ( $row as $v ) {
						$vals[] = ( null === $v ) ? 'NULL' : "'" . self::esc( $v ) . "'";
					}
					$line     = '(' . implode( ',', $vals ) . ')';
					$values[] = $line;
					$bytes   += strlen( $line );
					// Keep each INSERT around 1 MB so it fits MySQL's max_allowed_packet at restore.
					if ( $bytes >= self::INSERT_MAX_BYTES || count( $values ) >= self::ROWS_PER_INSERT ) {
						$out      = $head . implode( ",\n", $values ) . ";\n";
						$written += strlen( $out );
						gzwrite( $gz, $out );
						$values = array();
						$bytes  = 0;
					}
				}
				if ( $values ) {
					$out      = $head . implode( ",\n", $values ) . ";\n";
					$written += strlen( $out );
					gzwrite( $gz, $out );
				}
				$job['db']['rows']  += count( $rows );
				$job['db']['offset'] = $offset + count( $rows );
				if ( 1 === count( $pk ) ) {
					$job['db']['last'] = $rows[ count( $rows ) - 1 ][ $pk[0] ];
				}
			}

			if ( count( $rows ) < $limit ) {
				$job['db']['ti']++;
				$job['db']['offset'] = 0;
				unset( $job['db']['last'] );
			}
			unset( $rows );
		}

		if ( $job['db']['ti'] >= count( $tables ) ) {
			gzwrite( $gz, "\nSET FOREIGN_KEY_CHECKS = 1;\n-- Slut\n" );
		}
		gzclose( $gz );
		clearstatcache( true, $file );
		$job['db']['gz_size'] = (int) filesize( $file );

		if ( $job['db']['ti'] >= count( $tables ) ) {
			$job['outputs'][] = self::describe_output( $job['id'], 'database.sql.gz' );
			$job['phase']     = $job['options']['files'] ? 'scan' : 'finalize';
		}
		return $job;
	}

	/* -- Files --------------------------------------------------------- */

	private static function list_file( $id ) {
		return self::job_dir( $id ) . '/filelist.php';
	}

	private static function baseline_file() {
		return Sajtvakt_Core::data_dir( 'baseline.php' );
	}

	private static function pending_file( $id ) {
		return self::job_dir( $id ) . '/baseline-pending.php';
	}

	private static function step_scan( array $job ) {
		$max   = (int) $job['options']['max_file_mb'] * 1024 * 1024;
		$out   = fopen( self::list_file( $job['id'] ), 'wb' );
		$count = 0;
		$skip  = array();
		$all   = array(); // rel => array( size, mtime ) – the new baseline.

		// Incremental only when our saved baseline is exactly the backup the server has.
		$known = null;
		$base  = isset( $job['options']['base'] ) ? (string) $job['options']['base'] : '';
		if ( '' !== $base ) {
			$bl = Sajtvakt_Core::read_json( self::baseline_file() );
			if ( is_array( $bl ) && isset( $bl['job'], $bl['files'] ) && $bl['job'] === $base && is_array( $bl['files'] ) ) {
				$known = $bl['files'];
			}
		}
		$add = function ( $rel, $abs, $size, $mtime ) use ( $out, &$count, &$all, $known ) {
			$all[ $rel ] = array( (int) $size, (int) $mtime );
			if ( null !== $known && isset( $known[ $rel ] ) && (int) $known[ $rel ][0] === (int) $size && (int) $known[ $rel ][1] === (int) $mtime ) {
				return; // Unchanged since the last backup – the server already has it.
			}
			fwrite( $out, $rel . "\t" . $abs . "\t" . $size . "\n" );
			$count++;
		};

		fwrite( $out, Sajtvakt_Core::GUARD );

		// Root files needed to rebuild the site.
		$config = file_exists( ABSPATH . 'wp-config.php' ) ? ABSPATH . 'wp-config.php' : dirname( ABSPATH ) . '/wp-config.php';
		foreach ( array( $config, ABSPATH . '.htaccess', ABSPATH . '.user.ini', ABSPATH . 'robots.txt' ) as $abs ) {
			if ( is_file( $abs ) && is_readable( $abs ) ) {
				$add( 'root/' . basename( $abs ), $abs, filesize( $abs ), filemtime( $abs ) );
			}
		}

		$content = rtrim( wp_normalize_path( WP_CONTENT_DIR ), '/' );
		$dir_it  = new RecursiveDirectoryIterator( $content, FilesystemIterator::SKIP_DOTS | FilesystemIterator::UNIX_PATHS );
		$filter  = new RecursiveCallbackFilterIterator(
			$dir_it,
			function ( $current ) use ( $content ) {
				if ( $current->isDir() ) {
					$rel = substr( wp_normalize_path( $current->getPathname() ), strlen( $content ) + 1 );
					if ( false === strpos( $rel, '/' ) && in_array( $rel, self::$skip_dirs, true ) ) {
						return false;
					}
					return ! $current->isLink();
				}
				return true;
			}
		);
		$it = new RecursiveIteratorIterator( $filter, RecursiveIteratorIterator::LEAVES_ONLY, RecursiveIteratorIterator::CATCH_GET_CHILD );

		foreach ( $it as $file ) {
			if ( ! $file->isFile() || $file->isLink() ) {
				continue;
			}
			$abs  = wp_normalize_path( $file->getPathname() );
			$rel  = 'wp-content/' . substr( $abs, strlen( $content ) + 1 );
			$size = $file->getSize();
			if ( ! $file->isReadable() ) {
				$skip[] = array( 'file' => $rel, 'reason' => 'ej läsbar' );
				continue;
			}
			if ( $size > $max ) {
				$skip[] = array( 'file' => $rel, 'reason' => 'för stor (' . size_format( $size ) . ')' );
				continue;
			}
			if ( false !== strpos( $rel, "\t" ) || false !== strpos( $rel, "\n" ) ) {
				$skip[] = array( 'file' => $rel, 'reason' => 'ogiltigt filnamn' );
				continue;
			}
			$add( $rel, $abs, $size, $file->getMTime() );
		}
		fclose( $out );

		// The full list is kept as the baseline-to-be until the server confirms. The index the
		// server needs (index.json.gz) is written in finalize, from what was ACTUALLY zipped –
		// a file that vanishes between scan and zip must not stay in either list.
		Sajtvakt_Core::write_json( self::pending_file( $job['id'] ), array( 'job' => $job['id'], 'files' => $all ) );

		$job['files']['incremental'] = null !== $known;
		$job['files']['all']         = count( $all );
		$job['files']['total']   = $count;
		$job['files']['skipped'] = array_slice( $skip, 0, 200 );
		$job['phase']            = 'files';
		return $job;
	}

	private static function step_files( array $job, $deadline ) {
		$lines = file( self::list_file( $job['id'] ), FILE_IGNORE_NEW_LINES );
		array_shift( $lines ); // Guard line.
		$total = count( $lines );
		$index = (int) $job['files']['index'];

		if ( $index >= $total ) {
			$job['phase'] = 'finalize';
			return $job;
		}

		$job['files']['part']++;
		$name = sprintf( 'files-%03d.zip', $job['files']['part'] );
		$path = self::job_dir( $job['id'] ) . '/' . $name;

		$zip = new ZipArchive();
		if ( true !== $zip->open( $path, ZipArchive::CREATE | ZipArchive::OVERWRITE ) ) {
			throw new Exception( 'Kunde inte skapa zip-filen.' );
		}

		$bytes = 0;
		$added = 0;
		// Leave ~40% of the budget for ZipArchive::close(), where compression happens.
		$soft_deadline = microtime( true ) + max( 1, ( $deadline - microtime( true ) ) * 0.6 );
		while ( $index < $total ) {
			list( $rel, $abs, $size ) = array_pad( explode( "\t", $lines[ $index ], 3 ), 3, 0 );
			$size = (int) $size;
			if ( $added > 0 && ( $bytes + $size > self::PART_MAX_BYTES || $added >= self::PART_MAX_FILES || microtime( true ) > $soft_deadline ) ) {
				break;
			}
			if ( is_readable( $abs ) && $zip->addFile( $abs, $rel ) ) {
				$ext = strtolower( pathinfo( $rel, PATHINFO_EXTENSION ) );
				if ( in_array( $ext, self::$store_ext, true ) ) {
					$zip->setCompressionName( $rel, ZipArchive::CM_STORE );
				}
				$bytes += $size;
				$added++;
			} else {
				$job['files']['failed'][] = $rel;
				if ( count( $job['files']['skipped'] ) < 200 ) {
					$job['files']['skipped'][] = array( 'file' => $rel, 'reason' => 'försvann eller ej läsbar' );
				}
			}
			$index++;
		}

		if ( ! $zip->close() ) {
			throw new Exception( 'Kunde inte skriva zip-filen (fullt på disken?).' );
		}

		$job['files']['index']  = $index;
		$job['files']['bytes'] += $bytes;
		if ( $added > 0 ) {
			$job['outputs'][] = self::describe_output( $job['id'], $name );
		} else {
			@unlink( $path ); // phpcs:ignore
		}
		if ( $index >= $total ) {
			$job['phase'] = 'finalize';
		}
		return $job;
	}

	/* -- Finalize ------------------------------------------------------ */

	private static function finalize( array $job ) {
		global $wp_version;

		// Baslinjen och fillistan ska bara innehålla det som faktiskt packades.
		$failed  = isset( $job['files']['failed'] ) ? (array) $job['files']['failed'] : array();
		$pending = Sajtvakt_Core::read_json( self::pending_file( $job['id'] ) );
		if ( is_array( $pending ) && isset( $pending['files'] ) && is_array( $pending['files'] ) ) {
			foreach ( $failed as $rel ) {
				unset( $pending['files'][ $rel ] );
			}
			if ( $failed ) {
				Sajtvakt_Core::write_json( self::pending_file( $job['id'] ), $pending );
			}
			$job['files']['all'] = count( $pending['files'] );
			if ( ! empty( $job['files']['incremental'] ) ) {
				$rows = array();
				foreach ( $pending['files'] as $rel => $v ) {
					$rows[] = array( $rel, $v[0], $v[1] );
				}
				$gz = gzencode( json_encode( $rows, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE ), 6 );
				if ( false === $gz || false === file_put_contents( self::job_dir( $job['id'] ) . '/index.json.gz', $gz ) ) {
					throw new Exception( 'Kunde inte skriva fillistan.' );
				}
				unset( $rows, $gz );
				$job['outputs'][] = self::describe_output( $job['id'], 'index.json.gz' );
			}
		}

		$manifest = array(
			'format'    => 'sajtvakt-backup-1',
			'created'   => gmdate( 'c', $job['created'] ),
			'finished'  => gmdate( 'c' ),
			'site'      => home_url( '/' ),
			'siteurl'   => site_url( '/' ),
			'abspath'   => ABSPATH,
			'prefix'    => $GLOBALS['wpdb']->base_prefix,
			'wordpress' => $wp_version,
			'php'       => PHP_VERSION,
			'tables'    => count( $job['db']['tables'] ),
			'rows'      => $job['db']['rows'],
			'files'     => ! empty( $job['files']['all'] ) ? $job['files']['all'] : $job['files']['total'],
			'changed'   => $job['files']['total'],
			'incremental' => ! empty( $job['files']['incremental'] ),
			'base'      => ! empty( $job['files']['incremental'] ) ? $job['options']['base'] : null,
			'job'       => $job['id'],
			'skipped'   => $job['files']['skipped'],
			'outputs'   => $job['outputs'],
		);
		file_put_contents( self::job_dir( $job['id'] ) . '/manifest.json', json_encode( $manifest, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE ) );
		@unlink( self::list_file( $job['id'] ) ); // phpcs:ignore

		$job['outputs'][] = self::describe_output( $job['id'], 'manifest.json' );
		$job['phase']     = 'done';
		$job['status']    = 'done';

		$total = 0;
		foreach ( $job['outputs'] as $o ) {
			$total += $o['size'];
		}
		update_option(
			'sajtvakt_last_backup',
			array( 'time' => time(), 'job' => $job['id'], 'size' => $total, 'files' => $job['files']['total'], 'tables' => count( $job['db']['tables'] ) ),
			false
		);
		return $job;
	}

	private static function describe_output( $id, $name ) {
		$path = self::job_dir( $id ) . '/' . $name;
		clearstatcache( true, $path );
		return array(
			'name'   => $name,
			'size'   => (int) filesize( $path ),
			'sha256' => hash_file( 'sha256', $path ),
		);
	}

	/* ------------------------------------------------------------------ */
	/* Download / discard / finish                                         */
	/* ------------------------------------------------------------------ */

	private static function output_path( array $job, $name ) {
		foreach ( $job['outputs'] as $o ) {
			if ( $o['name'] === $name ) {
				return self::job_dir( $job['id'] ) . '/' . $name;
			}
		}
		return null;
	}

	public static function download( $id, $name, $offset, $length ) {
		$job  = self::load( $id );
		$path = $job ? self::output_path( $job, (string) $name ) : null;
		if ( ! $path || ! is_file( $path ) ) {
			Sajtvakt_Core::send_error( 'no_file', 'Filen finns inte i backupen.', 404 );
		}

		$size   = filesize( $path );
		$offset = max( 0, (int) $offset );
		$length = (int) $length > 0 ? min( (int) $length, self::DOWNLOAD_MAX ) : self::DOWNLOAD_MAX;
		$length = max( 0, min( $length, $size - $offset ) );

		while ( ob_get_level() > 0 ) {
			ob_end_clean();
		}
		@set_time_limit( 300 ); // phpcs:ignore
		http_response_code( 200 );
		header( 'Content-Type: application/octet-stream' );
		header( 'Cache-Control: no-store' );
		header( 'Content-Length: ' . $length );
		header( 'X-Sajtvakt-Size: ' . $size );
		header( 'X-Sajtvakt-Offset: ' . $offset );

		$fh = fopen( $path, 'rb' );
		fseek( $fh, $offset );
		$left = $length;
		while ( $left > 0 && ! feof( $fh ) ) {
			$buf   = fread( $fh, min( 1048576, $left ) );
			$left -= strlen( $buf );
			echo $buf; // phpcs:ignore
			flush();
		}
		fclose( $fh );
		exit;
	}

	public static function discard( $id, $name ) {
		$job  = self::load( $id );
		$path = $job ? self::output_path( $job, (string) $name ) : null;
		if ( $path && is_file( $path ) ) {
			@unlink( $path ); // phpcs:ignore
		}
		return array( 'ok' => true );
	}

	public static function finish( $id, $keep = false ) {
		if ( Sajtvakt_Core::valid_id( (string) $id ) ) {
			// The server stored this backup – it becomes the baseline for the next incremental one.
			// Only a job that actually completed may become the baseline.
			$job     = self::load( $id );
			$pending = self::pending_file( $id );
			if ( $keep && $job && 'done' === $job['status'] && is_file( $pending ) ) {
				@rename( $pending, self::baseline_file() ); // phpcs:ignore
			}
			Sajtvakt_Core::rrmdir( self::job_dir( $id ) );
			@unlink( self::job_file( $id ) ); // phpcs:ignore
			@unlink( self::job_file( $id ) . '.lock' ); // phpcs:ignore
		}
		return array( 'ok' => true );
	}
}
