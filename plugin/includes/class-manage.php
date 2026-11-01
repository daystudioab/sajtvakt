<?php
defined( 'ABSPATH' ) || exit;

/**
 * Avinstallera tillägg och teman från panelen. En kopia av filerna sparas först
 * (som vid uppdateringar), så att de kan läggas tillbaka med ett klick.
 */
final class Sajtvakt_Manage {

	public static function remove( array $params ) {
		Sajtvakt_Status::load_admin_includes();
		require_once ABSPATH . 'wp-admin/includes/file.php';

		if ( 'direct' !== get_filesystem_method( array(), WP_CONTENT_DIR ) ) {
			Sajtvakt_Core::send_error( 'fs_not_direct', 'WordPress kan inte skriva filer direkt på det här webbhotellet.', 500 );
		}
		if ( ! WP_Filesystem() ) {
			Sajtvakt_Core::send_error( 'fs_failed', 'Kunde inte starta WordPress filsystem.', 500 );
		}
		@set_time_limit( 300 ); // phpcs:ignore
		ignore_user_abort( true );

		$type = isset( $params['type'] ) ? (string) $params['type'] : '';
		$slug = isset( $params['slug'] ) ? (string) $params['slug'] : '';
		$ref  = isset( $params['ref'] ) ? (string) $params['ref'] : '';

		if ( 'plugin' === $type ) {
			$all = get_plugins();
			if ( ! isset( $all[ $slug ] ) ) {
				Sajtvakt_Core::send_error( 'not_found', 'Tillägget finns inte på sajten.', 404 );
			}
			if ( plugin_basename( SAJTVAKT_FILE ) === $slug ) {
				Sajtvakt_Core::send_error( 'self', 'Sajtvakt kan inte avinstallera sig självt härifrån.', 400 );
			}
			$name = $all[ $slug ]['Name'];
			$snap = Sajtvakt_Snapshot::create( array( $slug ), array(), $ref );
			if ( is_wp_error( $snap ) ) {
				Sajtvakt_Core::send_error( $snap->get_error_code(), $snap->get_error_message(), 500 );
			}
			if ( is_plugin_active( $slug ) ) {
				deactivate_plugins( $slug );
			}
			$res = delete_plugins( array( $slug ) );
			wp_clean_plugins_cache( false );
		} elseif ( 'theme' === $type ) {
			$theme = wp_get_theme( $slug );
			if ( ! $theme->exists() ) {
				Sajtvakt_Core::send_error( 'not_found', 'Temat finns inte på sajten.', 404 );
			}
			if ( get_stylesheet() === $slug || get_template() === $slug ) {
				Sajtvakt_Core::send_error( 'theme_in_use', 'Temat används av sajten (eller är förälder till det aktiva temat) och kan inte tas bort.', 400 );
			}
			$name = $theme->get( 'Name' );
			$snap = Sajtvakt_Snapshot::create( array(), array( $slug ), $ref );
			if ( is_wp_error( $snap ) ) {
				Sajtvakt_Core::send_error( $snap->get_error_code(), $snap->get_error_message(), 500 );
			}
			$res = delete_theme( $slug );
			wp_clean_themes_cache();
		} else {
			Sajtvakt_Core::send_error( 'bad_type', 'Okänd typ.', 400 );
		}

		if ( is_wp_error( $res ) || false === $res || null === $res ) {
			Sajtvakt_Core::send_error( 'remove_failed', 'Kunde inte ta bort ' . $name . ( is_wp_error( $res ) ? ': ' . $res->get_error_message() : '.' ), 500 );
		}
		Sajtvakt_Snapshot::mark_removed( $snap['id'] );

		return array(
			'ok'       => true,
			'name'     => $name,
			'snapshot' => $snap['id'],
			'check'    => Sajtvakt_Updater::check_site(),
		);
	}
}
