<?php
defined( 'ABSPATH' ) || exit;

/**
 * Wordfence: läser av senaste skanningen och kan starta en ny.
 *
 * Använder samma interna anrop som Wordfence Central (wfScanEngine::startScan,
 * wfIssues::getIssues). Allt är inlindat så att en ändring i Wordfence aldrig
 * kan få Sajtvakts eget anrop att fallera – då svarar vi bara "okänt".
 */
final class Sajtvakt_Security {

	const FILE = 'wordfence/wordfence.php';

	/** Wordfence är installerat (aktivt eller inte) – utan att ladda admin-filer i onödan. */
	private static function installed() {
		return is_file( WP_PLUGIN_DIR . '/' . self::FILE );
	}

	private static function loaded() {
		return class_exists( 'wfConfig' ) && class_exists( 'wfIssues' ) && class_exists( 'wfScanner' ) && class_exists( 'wfScanEngine' );
	}

	public static function status() {
		$out = array(
			'provider'  => 'wordfence',
			'installed' => self::installed(),
			'active'    => false,
			'checked'   => time(),
		);
		if ( ! $out['installed'] ) {
			return $out;
		}
		if ( ! function_exists( 'is_plugin_active' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}
		$out['active'] = is_plugin_active( self::FILE ) || ( is_multisite() && is_plugin_active_for_network( self::FILE ) );
		if ( defined( 'WORDFENCE_VERSION' ) ) {
			$out['version'] = WORDFENCE_VERSION;
		}
		if ( ! $out['active'] || ! self::loaded() ) {
			return $out;
		}

		try {
			$scanner = wfScanner::shared();
			$last    = $scanner->lastScanTime();
			$result  = wfConfig::get( 'lastScanCompleted' );

			$out['scan'] = array(
				'running'   => (bool) $scanner->isRunning(),
				'last'      => $last ? (int) $last : null,
				'result'    => ( false === $result || '' === $result ) ? null : ( 'ok' === $result ? 'ok' : wp_strip_all_tags( (string) $result ) ),
				'next'      => $scanner->nextScheduledScanTime() ? (int) $scanner->nextScheduledScanTime() : null,
				'scheduled' => (bool) $scanner->isEnabled(),
				'type'      => (string) $scanner->scanType(),
			);

			$issues = wfIssues::shared();
			$counts = array_merge( array( 'new' => 0, 'ignoreP' => 0, 'ignoreC' => 0 ), (array) $issues->getIssueCounts() );
			$list   = $issues->getIssues( 0, 25, 0, 0 );
			$items  = array();
			$sev    = array( 'critical' => 0, 'high' => 0, 'medium' => 0, 'low' => 0 );
			foreach ( (array) ( isset( $list['new'] ) ? $list['new'] : array() ) as $i ) {
				$s = (int) $i['severity'];
				$level = $s >= 100 ? 'critical' : ( $s >= 75 ? 'high' : ( $s >= 50 ? 'medium' : 'low' ) );
				$sev[ $level ]++;
				$items[] = array(
					'id'       => (int) $i['id'],
					'type'     => (string) $i['type'],
					'severity' => $level,
					'title'    => html_entity_decode( wp_strip_all_tags( (string) $i['shortMsg'] ), ENT_QUOTES, 'UTF-8' ),
					'time'     => (int) $i['time'],
				);
			}
			$out['issues'] = array(
				'new'      => (int) $counts['new'],
				'ignored'  => (int) $counts['ignoreP'] + (int) $counts['ignoreC'],
				'severity' => $sev,
				'items'    => $items,
			);

			if ( class_exists( 'wfFirewall' ) ) {
				$fw = new wfFirewall();
				$out['firewall'] = array(
					'mode'       => (string) $fw->firewallMode(),      // enabled | learning-mode | disabled
					'protection' => (string) $fw->protectionMode(),    // extended | basic
				);
			}
			$out['admin_url'] = admin_url( 'admin.php?page=WordfenceScan' );
		} catch ( \Throwable $e ) {
			$out['error'] = 'Kunde inte läsa Wordfence: ' . $e->getMessage();
		}
		return $out;
	}

	/** Startar en Wordfence-skanning. Själva skanningen kör sedan vidare i bakgrunden på sajten. */
	public static function scan() {
		if ( ! self::installed() ) {
			return array( 'ok' => true, 'started' => false, 'error' => 'Wordfence är inte installerat på sajten.' );
		}
		if ( ! self::loaded() ) {
			return array( 'ok' => true, 'started' => false, 'error' => 'Wordfence är inte aktiverat på sajten.' );
		}
		try {
			if ( wfScanner::shared()->isRunning() ) {
				return array( 'ok' => true, 'started' => false, 'running' => true, 'message' => 'En skanning pågår redan.' );
			}
			if ( function_exists( 'set_time_limit' ) ) {
				@set_time_limit( 90 ); // phpcs:ignore
			}
			wordfence::status( 1, 'info', 'Wordfence-skanning startad från Sajtvakt' );
			$err = wfScanEngine::startScan();
			if ( is_string( $err ) && '' !== $err ) {
				return array( 'ok' => true, 'started' => false, 'running' => wfScanner::shared()->isRunning(), 'message' => wp_strip_all_tags( $err ) );
			}
			return array( 'ok' => true, 'started' => true, 'running' => true );
		} catch ( \Throwable $e ) {
			if ( class_exists( 'wfScanEngine' ) && defined( 'wfScanEngine::SCAN_MANUALLY_KILLED' ) && $e->getCode() == wfScanEngine::SCAN_MANUALLY_KILLED ) { // phpcs:ignore
				return array( 'ok' => true, 'started' => false, 'error' => 'Skanningen stoppades.' );
			}
			if ( class_exists( 'wfConfig' ) ) {
				wfConfig::set( 'lastScanCompleted', $e->getMessage() );
			}
			if ( class_exists( 'wfUtils' ) && method_exists( 'wfUtils', 'clearScanLock' ) ) {
				wfUtils::clearScanLock();
			}
			return array( 'ok' => true, 'started' => false, 'error' => 'Wordfence kunde inte starta skanningen: ' . $e->getMessage() );
		}
	}
}
