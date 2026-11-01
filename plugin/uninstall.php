<?php
// Runs when the plugin is deleted in WordPress: remove everything Sajtvakt stored.
defined( 'WP_UNINSTALL_PLUGIN' ) || exit;

require_once __DIR__ . '/includes/class-core.php';

Sajtvakt_Core::rrmdir( WP_CONTENT_DIR . '/sajtvakt-data' );

$stub = ABSPATH . 'sajtvakt-rescue.php';
if ( is_file( $stub ) && false !== strpos( (string) file_get_contents( $stub ), 'Sajtvakt nödingång' ) ) {
	@unlink( $stub ); // phpcs:ignore
}

foreach ( array( 'sajtvakt_last_contact', 'sajtvakt_last_backup', 'sajtvakt_last_restore' ) as $option ) {
	delete_option( $option );
}
