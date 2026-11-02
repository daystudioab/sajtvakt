<?php
/**
 * Plugin Name:       Sajtvakt
 * Description:       Backup, uppdateringar och återställning – styrs från din egen Sajtvakt-server.
 * Version:           0.6.0
 * Plugin URI:        https://github.com/daystudioab/sajtvakt
 * Update URI:        https://github.com/daystudioab/sajtvakt
 * Requires at least: 6.0
 * Requires PHP:      7.4
 * Text Domain:       sajtvakt
 * License:           GPL-2.0-or-later
 */

defined( 'ABSPATH' ) || exit;

define( 'SAJTVAKT_FILE', __FILE__ );
define( 'SAJTVAKT_DIR', __DIR__ );

require_once SAJTVAKT_DIR . '/includes/class-core.php';
require_once SAJTVAKT_DIR . '/includes/class-status.php';
require_once SAJTVAKT_DIR . '/includes/class-security.php';
require_once SAJTVAKT_DIR . '/includes/class-backup.php';
require_once SAJTVAKT_DIR . '/includes/class-snapshot.php';
require_once SAJTVAKT_DIR . '/includes/class-updater.php';
require_once SAJTVAKT_DIR . '/includes/class-manage.php';
require_once SAJTVAKT_DIR . '/includes/class-restore.php';
require_once SAJTVAKT_DIR . '/includes/class-api.php';
require_once SAJTVAKT_DIR . '/includes/class-admin.php';
require_once SAJTVAKT_DIR . '/includes/class-self-update.php';

Sajtvakt_Core::set_data_dir( WP_CONTENT_DIR . '/sajtvakt-data' );

/**
 * Small glue class: activation, path bookkeeping and hook registration.
 */
final class Sajtvakt_Plugin {

	public static function init() {
		register_activation_hook( SAJTVAKT_FILE, array( __CLASS__, 'activate' ) );
		register_deactivation_hook( SAJTVAKT_FILE, array( __CLASS__, 'remove_rescue_stub' ) );

		add_action( 'plugins_loaded', array( __CLASS__, 'refresh_paths' ) );
		add_action( 'plugins_loaded', array( __CLASS__, 'ensure_rescue_stub' ) );

		// Primary endpoint: /wp-admin/admin-post.php?action=sajtvakt&op=...
		// Runs in admin context, so premium plugins register their updates.
		add_action( 'admin_post_nopriv_sajtvakt', array( 'Sajtvakt_Api', 'handle_admin_post' ) );
		add_action( 'admin_post_sajtvakt', array( 'Sajtvakt_Api', 'handle_admin_post' ) );

		// Fallback endpoint: /?sajtvakt-api=op (when wp-admin is locked down).
		add_action( 'wp_loaded', array( 'Sajtvakt_Api', 'maybe_handle_frontend' ), 0 );

		// Nya versioner av Sajtvakt kommer från GitHub (se class-self-update.php).
		Sajtvakt_Self_Update::init();

		if ( is_admin() ) {
			Sajtvakt_Admin::init();
		}
	}

	public static function activate() {
		Sajtvakt_Core::ensure_data_dir();
		if ( ! Sajtvakt_Core::get_key() ) {
			Sajtvakt_Core::generate_key();
		}
		self::refresh_paths( true );
		self::ensure_rescue_stub();
	}

	/* -------------------------------------------------------------- */
	/* Rescue entry in the WordPress root                              */
	/* -------------------------------------------------------------- */

	const RESCUE_STUB = 'sajtvakt-rescue.php';

	/**
	 * Many hosts refuse to run PHP files directly inside wp-content/plugins.
	 * A tiny file in the WordPress root (where PHP always runs) loads the real
	 * rescue.php instead. It does nothing without a valid signature.
	 */
	public static function rescue_stub_content() {
		return "<?php\n"
			. "// Sajtvakt nödingång – skapad automatiskt av tillägget Sajtvakt. Gör ingenting utan giltig signatur.\n"
			. "// Tas bort när tillägget inaktiveras.\n"
			. 'define( \'SAJTVAKT_RESCUE_DATA\', ' . var_export( Sajtvakt_Core::data_dir(), true ) . " );\n"
			. '$sajtvakt_rescue = ' . var_export( SAJTVAKT_DIR . '/rescue.php', true ) . ";\n"
			. "if ( is_file( \$sajtvakt_rescue ) ) {\n\trequire \$sajtvakt_rescue;\n} else {\n\thttp_response_code( 404 );\n}\n";
	}

	public static function rescue_stub_path() {
		return ABSPATH . self::RESCUE_STUB;
	}

	public static function ensure_rescue_stub() {
		$path    = self::rescue_stub_path();
		$content = self::rescue_stub_content();
		if ( is_file( $path ) && (string) @file_get_contents( $path ) === $content ) { // phpcs:ignore
			return;
		}
		if ( is_writable( ABSPATH ) || ( is_file( $path ) && is_writable( $path ) ) ) {
			@file_put_contents( $path, $content, LOCK_EX ); // phpcs:ignore
		}
	}

	public static function remove_rescue_stub() {
		$path = self::rescue_stub_path();
		if ( is_file( $path ) && false !== strpos( (string) @file_get_contents( $path ), 'Sajtvakt nödingång' ) ) { // phpcs:ignore
			@unlink( $path ); // phpcs:ignore
		}
	}

	/**
	 * Store the paths the stand-alone rescue file needs (it runs without WordPress).
	 */
	public static function refresh_paths( $force = false ) {
		$paths = array(
			'abspath'     => ABSPATH,
			'content_dir' => WP_CONTENT_DIR,
			'plugin_dir'  => WP_PLUGIN_DIR,
			'theme_roots' => array_values( array_unique( array_map( 'wp_normalize_path', (array) self::theme_roots() ) ) ),
		);
		$current = Sajtvakt_Core::read_paths();
		if ( $force || $current !== $paths ) {
			Sajtvakt_Core::ensure_data_dir();
			Sajtvakt_Core::write_paths( $paths );
		}
	}

	private static function theme_roots() {
		$roots = array( get_theme_root() );
		global $wp_theme_directories;
		if ( ! empty( $wp_theme_directories ) ) {
			$roots = array_merge( $roots, $wp_theme_directories );
		}
		return $roots;
	}
}

Sajtvakt_Plugin::init();
