<?php
/**
 * Nya versioner av Sajtvakt hämtas från GitHub-repot (daystudioab/sajtvakt).
 *
 * Repot innehåller dist/update.json med senaste version och en länk till zip-filen.
 * Den läggs in i WordPress vanliga uppdateringslista, så Sajtvakt-servern installerar
 * den på natten precis som andra tillägg – med ögonblicksbild och återställning.
 */

defined( 'ABSPATH' ) || exit;

final class Sajtvakt_Self_Update {

	const META      = 'https://raw.githubusercontent.com/daystudioab/sajtvakt/main/dist/update.json';
	const HOME      = 'https://github.com/daystudioab/sajtvakt';
	const CACHE     = 'sajtvakt_self_update';
	// Paketet måste komma från repot – inte från vilken adress som helst som står i filen.
	const ALLOWED   = array(
		'https://raw.githubusercontent.com/daystudioab/sajtvakt/',
		'https://github.com/daystudioab/sajtvakt/',
	);

	public static function init() {
		add_filter( 'upgrader_pre_download', array( __CLASS__, 'verify_download' ), 10, 3 );
		add_filter( 'pre_set_site_transient_update_plugins', array( __CLASS__, 'inject' ) );
		add_filter( 'plugins_api', array( __CLASS__, 'info' ), 10, 3 );
		add_filter( 'upgrader_source_selection', array( __CLASS__, 'keep_folder' ), 10, 4 );
		add_action( 'upgrader_process_complete', array( __CLASS__, 'forget' ) );
	}

	private static function basename() {
		return plugin_basename( SAJTVAKT_FILE );
	}

	/**
	 * Hämtar Sajtvakts eget uppdateringspaket och kontrollerar dess sha256 mot update.json
	 * innan WordPress får installera det. Utan kontrollen vore skrivåtkomst till repots
	 * main-gren detsamma som kodkörning på alla sajter.
	 */
	public static function verify_download( $reply, $package, $upgrader ) {
		if ( false !== $reply || ! is_string( $package ) ) {
			return $reply;
		}
		$ours = false;
		foreach ( self::ALLOWED as $prefix ) {
			if ( 0 === strpos( $package, $prefix ) ) {
				$ours = true;
				break;
			}
		}
		if ( ! $ours ) {
			return $reply;
		}
		$info = self::latest();
		if ( ! $info || empty( $info['sha256'] ) || ! isset( $info['package'] ) || $info['package'] !== $package ) {
			return $reply; // Ingen publicerad kontrollsumma för just det här paketet – låt WP sköta hämtningen.
		}
		if ( ! function_exists( 'download_url' ) ) {
			require_once ABSPATH . 'wp-admin/includes/file.php';
		}
		$file = download_url( $package, 300 );
		if ( is_wp_error( $file ) ) {
			return $file;
		}
		$hash = (string) hash_file( 'sha256', $file );
		if ( ! hash_equals( strtolower( (string) $info['sha256'] ), strtolower( $hash ) ) ) {
			@unlink( $file ); // phpcs:ignore
			return new WP_Error( 'sajtvakt_bad_hash', 'Sajtvakt-paketets kontrollsumma stämmer inte med update.json – uppdateringen avbröts.' );
		}
		return $file;
	}

	/** Senaste versionen enligt repot, sparad i sex timmar (en timme om GitHub inte svarade). */
	public static function latest( $fresh = false ) {
		$cached = get_site_transient( self::CACHE );
		if ( ! $fresh && false !== $cached ) {
			return is_array( $cached ) ? $cached : null;
		}
		$info = null;
		$res  = wp_remote_get( self::META, array( 'timeout' => 10, 'headers' => array( 'Accept' => 'application/json' ) ) );
		if ( ! is_wp_error( $res ) && 200 === (int) wp_remote_retrieve_response_code( $res ) ) {
			$data = json_decode( wp_remote_retrieve_body( $res ), true );
			if ( is_array( $data ) && self::valid( $data ) ) {
				$info = $data;
			}
		}
		set_site_transient( self::CACHE, $info ? $info : 'none', $info ? 6 * HOUR_IN_SECONDS : HOUR_IN_SECONDS );
		return $info;
	}

	private static function valid( array $d ) {
		if ( empty( $d['version'] ) || ! preg_match( '/^\d+\.\d+\.\d+$/', (string) $d['version'] ) ) {
			return false;
		}
		if ( empty( $d['package'] ) || ! is_string( $d['package'] ) ) {
			return false;
		}
		foreach ( self::ALLOWED as $prefix ) {
			if ( 0 === strpos( $d['package'], $prefix ) ) {
				return true;
			}
		}
		return false;
	}

	public static function inject( $transient ) {
		if ( ! is_object( $transient ) ) {
			return $transient;
		}
		$info = self::latest();
		if ( ! $info ) {
			return $transient;
		}
		$item = (object) array(
			'id'            => self::HOME,
			'slug'          => 'sajtvakt',
			'plugin'        => self::basename(),
			'new_version'   => $info['version'],
			'url'           => self::HOME,
			'package'       => $info['package'],
			'requires'      => isset( $info['requires'] ) ? $info['requires'] : '6.0',
			'requires_php'  => isset( $info['requires_php'] ) ? $info['requires_php'] : '7.4',
			'tested'        => isset( $info['tested'] ) ? $info['tested'] : '',
			'icons'         => array(),
			'banners'       => array(),
		);
		if ( version_compare( $info['version'], Sajtvakt_Core::VERSION, '>' ) ) {
			$transient->response[ self::basename() ] = $item;
			unset( $transient->no_update[ self::basename() ] );
		} else {
			$item->new_version = Sajtvakt_Core::VERSION;
			$transient->no_update[ self::basename() ] = $item;
			unset( $transient->response[ self::basename() ] );
		}
		return $transient;
	}

	/** "Visa detaljer" i tilläggslistan. */
	public static function info( $result, $action, $args ) {
		if ( 'plugin_information' !== $action || empty( $args->slug ) || 'sajtvakt' !== $args->slug ) {
			return $result;
		}
		$info = self::latest();
		if ( ! $info ) {
			return $result;
		}
		$changes = isset( $info['changes'] ) && is_array( $info['changes'] ) ? $info['changes'] : array();
		$list    = $changes ? '<ul><li>' . implode( '</li><li>', array_map( 'esc_html', $changes ) ) . '</li></ul>' : '';
		return (object) array(
			'name'          => 'Sajtvakt',
			'slug'          => 'sajtvakt',
			'version'       => $info['version'],
			'author'        => 'Day Studio',
			'homepage'      => self::HOME,
			'download_link' => $info['package'],
			'requires'      => isset( $info['requires'] ) ? $info['requires'] : '6.0',
			'requires_php'  => isset( $info['requires_php'] ) ? $info['requires_php'] : '7.4',
			'tested'        => isset( $info['tested'] ) ? $info['tested'] : '',
			'last_updated'  => isset( $info['date'] ) ? $info['date'] : '',
			'sections'      => array(
				'description' => '<p>Backup, uppdateringar och återställning – styrs från din egen Sajtvakt-server.</p>',
				'changelog'   => '<h4>' . esc_html( $info['version'] ) . '</h4>' . $list,
			),
		);
	}

	/** Zip-filen innehåller mappen "sajtvakt" – ligger tillägget i en annan mapp behålls den. */
	public static function keep_folder( $source, $remote_source, $upgrader, $hook_extra = array() ) {
		global $wp_filesystem;
		if ( empty( $hook_extra['plugin'] ) || self::basename() !== $hook_extra['plugin'] || ! $wp_filesystem ) {
			return $source;
		}
		$want = trailingslashit( $remote_source ) . dirname( self::basename() );
		if ( untrailingslashit( $source ) === $want ) {
			return $source;
		}
		if ( $wp_filesystem->move( untrailingslashit( $source ), $want, true ) ) {
			return trailingslashit( $want );
		}
		return $source;
	}

	public static function forget() {
		delete_site_transient( self::CACHE );
	}
}
