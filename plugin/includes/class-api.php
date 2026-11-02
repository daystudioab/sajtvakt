<?php
defined( 'ABSPATH' ) || exit;

/**
 * The single entry point for the Sajtvakt server. Every call is a POST with
 * an HMAC-signed body; there is no way in without the site's key.
 */
final class Sajtvakt_Api {

	/** Operations that take a raw (binary) body; their params come in a signed header. */
	private static $raw_body_ops = array( 'restore_upload' );

	public static function handle_admin_post() {
		$op = isset( $_GET['op'] ) ? sanitize_key( wp_unslash( $_GET['op'] ) ) : ''; // phpcs:ignore
		self::dispatch( $op );
	}

	public static function maybe_handle_frontend() {
		if ( ! isset( $_GET['sajtvakt-api'] ) ) { // phpcs:ignore
			return;
		}
		self::dispatch( sanitize_key( wp_unslash( $_GET['sajtvakt-api'] ) ) ); // phpcs:ignore
	}

	private static function dispatch( $op ) {
		ob_start(); // Swallow stray output from other plugins.

		if ( 'POST' !== ( isset( $_SERVER['REQUEST_METHOD'] ) ? $_SERVER['REQUEST_METHOD'] : '' ) ) {
			Sajtvakt_Core::send_error( 'method', 'Endast POST.', 405 );
		}

		$body    = (string) file_get_contents( 'php://input' );
		$headers = Sajtvakt_Core::request_headers();
		$valid   = Sajtvakt_Core::verify_request( $op, $body, $headers );
		if ( true !== $valid ) {
			// Same answer for every auth failure (except clock skew, which helps debugging).
			if ( 'bad_timestamp' === $valid ) {
				Sajtvakt_Core::send_error( 'bad_timestamp', 'Klockan skiljer sig för mycket mellan servern och sajten.', 401, array( 'site_time' => time() ) );
			}
			Sajtvakt_Core::send_error( 'unauthorized', 'Ogiltig eller saknad signatur.', 401 );
		}

		if ( in_array( $op, self::$raw_body_ops, true ) ) {
			// Params travel base64url-encoded in a signed header (safe from quoting/magic quotes).
			$params = json_decode( (string) base64_decode( strtr( $headers['params'], '-_', '+/' ) ), true );
		} else {
			$params = '' === $body ? array() : json_decode( $body, true );
		}
		if ( ! is_array( $params ) ) {
			Sajtvakt_Core::send_error( 'bad_json', 'Ogiltig JSON i anropet.', 400 );
		}

		update_option( 'sajtvakt_last_contact', time(), false );

		$p = function ( $key, $default = null ) use ( $params ) {
			return array_key_exists( $key, $params ) ? $params[ $key ] : $default;
		};

		switch ( $op ) {
			case 'ping':
				global $wp_version;
				$out = array(
					'ok'       => true,
					'sajtvakt' => Sajtvakt_Core::VERSION,
					'wp'       => $wp_version,
					'home'     => home_url( '/' ),
					'rescue'   => plugins_url( 'rescue.php', SAJTVAKT_FILE ),
					'rescue_alt' => is_file( Sajtvakt_Plugin::rescue_stub_path() ) ? site_url( '/' . Sajtvakt_Plugin::RESCUE_STUB ) : null,
					'time'     => time(),
				);
				break;

			case 'status':
				$out = Sajtvakt_Status::collect( (bool) $p( 'refresh', false ) );
				$out['environment']['chunk_size'] = Sajtvakt_Restore::chunk_size();
				$out['sajtvakt']['rescue']        = plugins_url( 'rescue.php', SAJTVAKT_FILE );
				$out['sajtvakt']['rescue_alt']    = is_file( Sajtvakt_Plugin::rescue_stub_path() ) ? site_url( '/' . Sajtvakt_Plugin::RESCUE_STUB ) : null;
				break;

			case 'security':
				$out = array( 'ok' => true, 'security' => Sajtvakt_Security::status() );
				break;
			case 'security_scan':
				$out = Sajtvakt_Security::scan();
				$out['security'] = Sajtvakt_Security::status();
				break;

			case 'backup_start':
				$out = Sajtvakt_Backup::start( $params );
				break;
			case 'backup_step':
				$out = Sajtvakt_Backup::step( $p( 'job' ), $p( 'budget', 20 ) );
				break;
			case 'backup_download':
				Sajtvakt_Backup::download( $p( 'job' ), $p( 'file' ), $p( 'offset', 0 ), $p( 'length', 0 ) );
				exit; // download() streams and exits.
			case 'backup_discard':
				$out = Sajtvakt_Backup::discard( $p( 'job' ), $p( 'file' ) );
				break;
			case 'backup_finish':
				$out = Sajtvakt_Backup::finish( $p( 'job' ), (bool) $p( 'keep', false ) );
				break;

			case 'update':
				$out = Sajtvakt_Updater::run( $params );
				break;
			case 'remove':
				$out = Sajtvakt_Manage::remove( $params );
				break;
			case 'check':
				$out = array( 'ok' => true, 'check' => Sajtvakt_Updater::check_site() );
				break;
			case 'snapshots':
				$out = array( 'ok' => true, 'snapshots' => Sajtvakt_Core::list_snapshots() );
				break;
			case 'rollback':
				$snap = (string) $p( 'snapshot', '' );
				if ( '' === $snap && $p( 'ref' ) ) {
					$snap = (string) Sajtvakt_Core::snapshot_by_ref( (string) $p( 'ref' ) );
				}
				$out             = Sajtvakt_Core::rollback_snapshot( $snap );
				$out['snapshot'] = $snap;
				wp_clean_plugins_cache( false );
				wp_clean_themes_cache();
				break;

			case 'restore_start':
				$out = Sajtvakt_Restore::start( $params );
				break;
			case 'restore_upload':
				$out = Sajtvakt_Restore::upload( $params, $body );
				break;
			case 'restore_step':
				$out = Sajtvakt_Restore::step( $p( 'job' ), $p( 'budget', 20 ) );
				break;
			case 'restore_status':
				$out = Sajtvakt_Restore::status( $p( 'job' ) );
				break;
			case 'restore_cancel':
				$out = Sajtvakt_Restore::cancel( $p( 'job' ) );
				break;

			default:
				Sajtvakt_Core::send_error( 'unknown_op', 'Okänd åtgärd.', 404 );
		}

		Sajtvakt_Core::send_json( $out, ( isset( $out['ok'] ) && false === $out['ok'] ) ? 500 : 200 );
	}
}
