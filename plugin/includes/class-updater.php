<?php
defined( 'ABSPATH' ) || exit;

/**
 * Updates plugins, themes, translations and core – after saving a snapshot of
 * every plugin/theme folder that is about to change.
 */
final class Sajtvakt_Updater {

	public static function run( array $params ) {
		Sajtvakt_Status::load_admin_includes();
		require_once ABSPATH . 'wp-admin/includes/class-wp-upgrader.php';

		if ( 'direct' !== get_filesystem_method( array(), WP_CONTENT_DIR ) ) {
			Sajtvakt_Core::send_error( 'fs_not_direct', 'WordPress kan inte skriva filer direkt på det här webbhotellet (kräver FTP-uppgifter). Uppdateringar via Sajtvakt fungerar inte här.', 500 );
		}
		if ( ! WP_Filesystem() ) {
			Sajtvakt_Core::send_error( 'fs_failed', 'Kunde inte starta WordPress filsystem.', 500 );
		}

		@set_time_limit( 600 ); // phpcs:ignore
		ignore_user_abort( true );

		if ( ! isset( $params['refresh'] ) || $params['refresh'] ) {
			Sajtvakt_Status::refresh_update_data();
		}

		$all_plugins = get_plugins();
		$plugins     = array();
		$themes      = array();
		$core        = false;
		$languages   = false;

		foreach ( (array) ( isset( $params['items'] ) ? $params['items'] : array() ) as $item ) {
			$type = isset( $item['type'] ) ? $item['type'] : '';
			$slug = isset( $item['slug'] ) ? (string) $item['slug'] : '';
			if ( 'plugin' === $type && isset( $all_plugins[ $slug ] ) ) {
				$plugins[] = $slug;
			} elseif ( 'theme' === $type && wp_get_theme( $slug )->exists() ) {
				$themes[] = $slug;
			} elseif ( 'core' === $type ) {
				$core = true;
			} elseif ( 'translations' === $type ) {
				$languages = true;
			}
		}
		$plugins = array_values( array_unique( $plugins ) );
		$themes  = array_values( array_unique( $themes ) );

		if ( ! $plugins && ! $themes && ! $core && ! $languages ) {
			Sajtvakt_Core::send_error( 'nothing', 'Inget giltigt att uppdatera angavs.', 400 );
		}

		// 1. Snapshot first. No snapshot, no update.
		$snapshot = null;
		if ( $plugins || $themes ) {
			$snap = Sajtvakt_Snapshot::create( $plugins, $themes, isset( $params['ref'] ) ? (string) $params['ref'] : '' );
			if ( is_wp_error( $snap ) ) {
				Sajtvakt_Core::send_error( $snap->get_error_code(), $snap->get_error_message(), 500 );
			}
			$snapshot = $snap['id'];
		}

		$active_before = (array) get_option( 'active_plugins', array() );
		$results       = array();
		$after         = array();

		// 2. Plugins.
		if ( $plugins ) {
			$skin     = new WP_Ajax_Upgrader_Skin();
			$upgrader = new Plugin_Upgrader( $skin );
			$res      = $upgrader->bulk_upgrade( $plugins, array( 'clear_update_cache' => true ) );
			wp_clean_plugins_cache( false );
			$now = get_plugins();
			foreach ( $plugins as $file ) {
				$r       = is_array( $res ) && array_key_exists( $file, $res ) ? $res[ $file ] : null;
				$before  = $all_plugins[ $file ]['Version'];
				$version = isset( $now[ $file ] ) ? $now[ $file ]['Version'] : null;
				$after[ 'plugin:' . $file ] = $version;
				$results[] = self::result_row( 'plugin', $file, $all_plugins[ $file ]['Name'], $before, $version, $r, $skin );
			}
		}

		// Plugins that were active must stay active.
		$reactivated = array();
		foreach ( $active_before as $file ) {
			if ( ! is_plugin_active( $file ) && file_exists( WP_PLUGIN_DIR . '/' . $file ) ) {
				$r = activate_plugin( $file, '', false, true );
				if ( ! is_wp_error( $r ) ) {
					$reactivated[] = $file;
				}
			}
		}

		// 3. Themes.
		if ( $themes ) {
			$skin     = new WP_Ajax_Upgrader_Skin();
			$upgrader = new Theme_Upgrader( $skin );
			$before   = array();
			foreach ( $themes as $slug ) {
				$before[ $slug ] = wp_get_theme( $slug )->get( 'Version' );
			}
			$res = $upgrader->bulk_upgrade( $themes, array( 'clear_update_cache' => true ) );
			wp_clean_themes_cache();
			foreach ( $themes as $slug ) {
				$theme   = wp_get_theme( $slug );
				$version = $theme->exists() ? $theme->get( 'Version' ) : null;
				$r       = is_array( $res ) && array_key_exists( $slug, $res ) ? $res[ $slug ] : null;
				$after[ 'theme:' . $slug ] = $version;
				$results[] = self::result_row( 'theme', $slug, $theme->get( 'Name' ), $before[ $slug ], $version, $r, $skin );
			}
		}

		if ( $snapshot ) {
			Sajtvakt_Snapshot::mark_updated( $snapshot, $after );
		}

		// 4. Translations.
		if ( $languages ) {
			$skin     = new WP_Ajax_Upgrader_Skin();
			$upgrader = new Language_Pack_Upgrader( $skin );
			$pending  = wp_get_translation_updates();
			$res      = $pending ? $upgrader->bulk_upgrade( $pending ) : array();
			$results[] = array(
				'type'    => 'translations',
				'slug'    => 'translations',
				'name'    => 'Översättningar',
				'ok'      => ! is_wp_error( $res ) && false !== $res,
				'status'  => $pending ? 'updated' : 'up_to_date',
				'count'   => count( $pending ),
				'message' => self::errors_text( $skin ),
			);
		}

		// 5. Core (no file snapshot – a database backup before is the safety net).
		if ( $core ) {
			global $wp_version;
			$before = $wp_version;
			$offer  = null;
			foreach ( (array) get_core_updates() as $u ) {
				if ( isset( $u->response ) && 'upgrade' === $u->response ) {
					$offer = $u;
					break;
				}
			}
			if ( ! $offer ) {
				$results[] = array( 'type' => 'core', 'slug' => 'wordpress', 'name' => 'WordPress', 'ok' => true, 'status' => 'up_to_date', 'before' => $before, 'after' => $before );
			} else {
				$skin     = new WP_Ajax_Upgrader_Skin();
				$upgrader = new Core_Upgrader( $skin );
				$res      = $upgrader->upgrade( $offer, array( 'attempt_rollback' => true ) );
				$ok       = ! is_wp_error( $res ) && false !== $res;
				$results[] = array(
					'type'    => 'core',
					'slug'    => 'wordpress',
					'name'    => 'WordPress',
					'ok'      => $ok,
					'status'  => $ok ? 'updated' : 'failed',
					'before'  => $before,
					'after'   => $ok ? (string) $res : $before,
					'message' => is_wp_error( $res ) ? $res->get_error_message() : self::errors_text( $skin ),
				);
			}
		}

		return array(
			'ok'          => ! in_array( false, wp_list_pluck( $results, 'ok' ), true ),
			'snapshot'    => $snapshot,
			'results'     => $results,
			'reactivated' => $reactivated,
			'check'       => self::check_site(),
		);
	}

	private static function result_row( $type, $slug, $name, $before, $after, $raw, $skin ) {
		$error = is_wp_error( $raw ) ? $raw->get_error_message() : '';
		if ( '' === $error ) {
			$error = self::errors_text( $skin );
		}
		if ( $after && $before && version_compare( $after, $before, '>' ) ) {
			$status = 'updated';
		} elseif ( $raw && ! is_wp_error( $raw ) ) {
			$status = 'updated';
		} elseif ( null === $raw || ( false === $raw && '' === $error ) ) {
			$status = 'up_to_date';
		} else {
			$status = 'failed';
		}
		return array(
			'type'    => $type,
			'slug'    => $slug,
			'name'    => $name,
			'ok'      => 'failed' !== $status,
			'status'  => $status,
			'before'  => $before,
			'after'   => $after,
			'message' => 'failed' === $status ? ( $error ? $error : 'Uppdateringen misslyckades.' ) : '',
		);
	}

	private static function errors_text( $skin ) {
		if ( $skin instanceof WP_Ajax_Upgrader_Skin ) {
			$e = $skin->get_errors();
			if ( $e->has_errors() ) {
				return implode( ' ', $e->get_error_messages() );
			}
		}
		return '';
	}

	/**
	 * Load the home page in a new request (new code) and see if it still works.
	 */
	public static function check_site() {
		$url = add_query_arg( 'sajtvakt-check', time(), home_url( '/' ) );
		$res = wp_remote_get(
			$url,
			array(
				'timeout'     => 20,
				'redirection' => 3,
				'sslverify'   => apply_filters( 'https_local_ssl_verify', false ),
				'headers'     => array( 'Cache-Control' => 'no-cache' ),
			)
		);
		if ( is_wp_error( $res ) ) {
			return array( 'ok' => null, 'message' => 'Sajten kunde inte nås inifrån: ' . $res->get_error_message() );
		}
		$code   = (int) wp_remote_retrieve_response_code( $res );
		$body   = (string) wp_remote_retrieve_body( $res );
		$broken = $code >= 500 || false !== stripos( $body, 'id="error-page"' ) || false !== stripos( $body, 'wp-die-message' );
		return array( 'ok' => ! $broken, 'code' => $code, 'bytes' => strlen( $body ) );
	}
}
