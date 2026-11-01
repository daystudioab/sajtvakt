<?php
/**
 * Sajtvakt nödingång.
 *
 * Runs WITHOUT WordPress, so it still works when an update has crashed the
 * site. It can only do a few safe things, and only with a valid signature:
 *   ping, snapshots, rollback, clear_maintenance
 */

define( 'SAJTVAKT_RESCUE', true );
require __DIR__ . '/includes/class-core.php';

if ( ! defined( 'DAY_SECONDS_FALLBACK' ) ) {
	define( 'DAY_SECONDS_FALLBACK', 86400 );
}

// Called via the stub in the WordPress root, the data folder is passed in.
// Otherwise: wp-content/plugins/sajtvakt/rescue.php → wp-content/sajtvakt-data
$candidates = array( dirname( __DIR__, 2 ) . '/sajtvakt-data' );
if ( defined( 'SAJTVAKT_RESCUE_DATA' ) ) {
	array_unshift( $candidates, SAJTVAKT_RESCUE_DATA );
}
foreach ( $candidates as $dir ) {
	if ( is_dir( $dir ) ) {
		Sajtvakt_Core::set_data_dir( $dir );
		break;
	}
}
if ( '' === Sajtvakt_Core::data_dir() ) {
	Sajtvakt_Core::send_error( 'no_data_dir', 'Hittar inte Sajtvakts datamapp.', 500 );
}

if ( 'POST' !== ( isset( $_SERVER['REQUEST_METHOD'] ) ? $_SERVER['REQUEST_METHOD'] : '' ) ) {
	Sajtvakt_Core::send_error( 'method', 'Endast POST.', 405 );
}

$op      = isset( $_GET['op'] ) ? preg_replace( '/[^a-z_]/', '', strtolower( (string) $_GET['op'] ) ) : '';
$body    = (string) file_get_contents( 'php://input' );
$headers = Sajtvakt_Core::request_headers();
$valid   = Sajtvakt_Core::verify_request( 'rescue_' . $op, $body, $headers );
if ( true !== $valid ) {
	if ( 'bad_timestamp' === $valid ) {
		Sajtvakt_Core::send_error( 'bad_timestamp', 'Klockan skiljer sig för mycket mellan servern och sajten.', 401, array( 'site_time' => time() ) );
	}
	Sajtvakt_Core::send_error( 'unauthorized', 'Ogiltig eller saknad signatur.', 401 );
}

$params = '' === $body ? array() : json_decode( $body, true );
if ( ! is_array( $params ) ) {
	Sajtvakt_Core::send_error( 'bad_json', 'Ogiltig JSON i anropet.', 400 );
}

switch ( $op ) {
	case 'ping':
		Sajtvakt_Core::send_json( array( 'ok' => true, 'rescue' => true, 'sajtvakt' => Sajtvakt_Core::VERSION, 'time' => time() ) );
		break;

	case 'snapshots':
		Sajtvakt_Core::send_json( array( 'ok' => true, 'snapshots' => Sajtvakt_Core::list_snapshots() ) );
		break;

	case 'rollback':
		$id = isset( $params['snapshot'] ) ? (string) $params['snapshot'] : '';
		if ( '' === $id && ! empty( $params['ref'] ) ) {
			$id = (string) Sajtvakt_Core::snapshot_by_ref( (string) $params['ref'] );
		}
		if ( 'latest' === $id ) {
			$list = Sajtvakt_Core::list_snapshots();
			$id   = $list ? $list[0]['id'] : '';
		}
		$res = Sajtvakt_Core::rollback_snapshot( $id );
		$res['snapshot'] = $id;
		Sajtvakt_Core::send_json( $res, $res['ok'] ? 200 : 500 );
		break;

	case 'clear_maintenance':
		Sajtvakt_Core::send_json( array( 'ok' => true, 'removed' => Sajtvakt_Core::clear_maintenance() ) );
		break;

	default:
		Sajtvakt_Core::send_error( 'unknown_op', 'Okänd åtgärd.', 404 );
}
