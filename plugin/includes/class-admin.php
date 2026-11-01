<?php
defined( 'ABSPATH' ) || exit;

/**
 * Verktyg → Sajtvakt: connection key, status and manual rollback.
 */
final class Sajtvakt_Admin {

	const SLUG = 'sajtvakt';

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'menu' ) );
		add_action( 'admin_post_sajtvakt_admin', array( __CLASS__, 'handle_form' ) );
		add_filter( 'plugin_action_links_' . plugin_basename( SAJTVAKT_FILE ), array( __CLASS__, 'action_links' ) );
	}

	public static function menu() {
		add_management_page( 'Sajtvakt', 'Sajtvakt', 'manage_options', self::SLUG, array( __CLASS__, 'render' ) );
	}

	public static function action_links( $links ) {
		array_unshift( $links, '<a href="' . esc_url( admin_url( 'tools.php?page=' . self::SLUG ) ) . '">Anslut</a>' );
		return $links;
	}

	public static function connection_string() {
		$key = Sajtvakt_Core::get_key();
		if ( ! $key ) {
			return '';
		}
		$payload = wp_json_encode( array( 'u' => home_url( '/' ), 'k' => $key ) );
		return 'sv1:' . rtrim( strtr( base64_encode( $payload ), '+/', '-_' ), '=' );
	}

	public static function handle_form() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Behörighet saknas.' );
		}
		check_admin_referer( 'sajtvakt_admin' );
		$do  = isset( $_POST['do'] ) ? sanitize_key( wp_unslash( $_POST['do'] ) ) : '';
		$msg = '';

		if ( 'regenerate' === $do ) {
			Sajtvakt_Core::generate_key();
			$msg = 'new_key';
		} elseif ( 'rollback' === $do && isset( $_POST['snapshot'] ) ) {
			$res = Sajtvakt_Core::rollback_snapshot( sanitize_text_field( wp_unslash( $_POST['snapshot'] ) ) );
			$msg = $res['ok'] ? 'rolled_back' : 'rollback_failed';
		}
		wp_safe_redirect( admin_url( 'tools.php?page=' . self::SLUG . '&sv_msg=' . $msg ) );
		exit;
	}

	private static function ago( $ts ) {
		return $ts ? sprintf( '%s sedan', human_time_diff( (int) $ts ) ) : 'aldrig';
	}

	public static function render() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}
		Sajtvakt_Core::ensure_data_dir();
		if ( ! Sajtvakt_Core::get_key() ) {
			Sajtvakt_Core::generate_key();
		}

		$conn         = self::connection_string();
		$last_contact = get_option( 'sajtvakt_last_contact' );
		$last_backup  = get_option( 'sajtvakt_last_backup' );
		$last_restore = get_option( 'sajtvakt_last_restore' );
		$snapshots    = Sajtvakt_Core::list_snapshots();
		$writable     = is_writable( Sajtvakt_Core::data_dir() );

		require_once ABSPATH . 'wp-admin/includes/file.php';
		$fs_direct = ( 'direct' === get_filesystem_method( array(), WP_CONTENT_DIR ) );

		$messages = array(
			'new_key'         => array( 'success', 'En ny nyckel är skapad. Den gamla slutar fungera direkt – klistra in den nya i Sajtvakt-servern.' ),
			'rolled_back'     => array( 'success', 'Återställt till läget före uppdateringen.' ),
			'rollback_failed' => array( 'error', 'Återställningen misslyckades. Se loggen på servern.' ),
		);
		$msg = isset( $_GET['sv_msg'] ) ? sanitize_key( wp_unslash( $_GET['sv_msg'] ) ) : ''; // phpcs:ignore
		?>
		<div class="wrap sajtvakt">
			<h1>Sajtvakt</h1>
			<p class="description">Backup, uppdateringar och återställning – styrs från din Sajtvakt-server.</p>

			<?php if ( isset( $messages[ $msg ] ) ) : ?>
				<div class="notice notice-<?php echo esc_attr( $messages[ $msg ][0] ); ?> is-dismissible"><p><?php echo esc_html( $messages[ $msg ][1] ); ?></p></div>
			<?php endif; ?>

			<?php if ( ! $writable ) : ?>
				<div class="notice notice-error"><p>Sajtvakt kan inte skriva i <code>wp-content/sajtvakt-data</code>. Kontrollera rättigheterna hos webbhotellet.</p></div>
			<?php endif; ?>
			<?php if ( ! $fs_direct ) : ?>
				<div class="notice notice-warning"><p>WordPress kan inte skriva filer direkt här (kräver FTP-uppgifter). Backup fungerar, men uppdateringar via Sajtvakt gör det inte.</p></div>
			<?php endif; ?>

			<div class="sv-card">
				<h2>Anslutningsnyckel</h2>
				<p>Kopiera nyckeln och klistra in den när du lägger till sajten i Sajtvakt-servern. Den ger full kontroll över backup och uppdateringar – behandla den som ett lösenord.</p>
				<div class="sv-key">
					<input type="password" id="sv-conn" class="large-text code" readonly value="<?php echo esc_attr( $conn ); ?>">
					<button type="button" class="button" id="sv-show">Visa</button>
					<button type="button" class="button button-primary" id="sv-copy">Kopiera</button>
				</div>
				<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" onsubmit="return confirm('Skapa en ny nyckel? Den gamla slutar fungera direkt.');">
					<?php wp_nonce_field( 'sajtvakt_admin' ); ?>
					<input type="hidden" name="action" value="sajtvakt_admin">
					<input type="hidden" name="do" value="regenerate">
					<p><button class="button-link sv-danger" type="submit">Skapa ny nyckel</button></p>
				</form>
			</div>

			<div class="sv-card">
				<h2>Status</h2>
				<table class="widefat striped sv-table">
					<tbody>
						<tr><th>Senaste kontakt med servern</th><td><?php echo esc_html( self::ago( $last_contact ) ); ?></td></tr>
						<tr><th>Senaste backup</th><td>
							<?php
							if ( $last_backup ) {
								echo esc_html( self::ago( $last_backup['time'] ) . ' · ' . size_format( $last_backup['size'] ) . ' · ' . number_format_i18n( $last_backup['files'] ) . ' filer, ' . $last_backup['tables'] . ' tabeller' );
							} else {
								echo 'aldrig';
							}
							?>
						</td></tr>
						<?php if ( $last_restore ) : ?>
							<tr><th>Senaste återställning</th><td><?php echo esc_html( self::ago( $last_restore['time'] ) . ( 'migrate' === $last_restore['mode'] ? ' · flyttad från ' . $last_restore['from'] : '' ) ); ?></td></tr>
						<?php endif; ?>
						<tr><th>Version</th><td><?php echo esc_html( Sajtvakt_Core::VERSION ); ?></td></tr>
					</tbody>
				</table>
			</div>

			<div class="sv-card">
				<h2>Ögonblicksbilder före uppdateringar</h2>
				<?php if ( ! $snapshots ) : ?>
					<p>Inga ännu. Före varje uppdatering sparas en kopia av det som uppdateras, så att det kan återställas med ett klick.</p>
				<?php else : ?>
					<table class="widefat striped sv-table">
						<thead><tr><th>När</th><th>Vad</th><th></th></tr></thead>
						<tbody>
						<?php foreach ( $snapshots as $s ) : ?>
							<tr>
								<td><?php echo esc_html( wp_date( 'Y-m-d H:i', $s['created'] ) ); ?></td>
								<td>
									<?php
									$parts = array();
									foreach ( $s['items'] as $i ) {
										$parts[] = $i['name'] . ' ' . $i['before'] . ( $i['after'] && $i['after'] !== $i['before'] ? ' → ' . $i['after'] : '' );
									}
									echo esc_html( implode( ', ', $parts ) );
									if ( $s['rolled_back'] ) {
										echo ' <em>(återställd ' . esc_html( self::ago( $s['rolled_back'] ) ) . ')</em>';
									}
									?>
								</td>
								<td class="sv-right">
									<?php if ( in_array( $s['status'], array( 'ready', 'updated' ), true ) ) : ?>
									<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" onsubmit="return confirm('Återställa till versionerna före den här uppdateringen?');">
										<?php wp_nonce_field( 'sajtvakt_admin' ); ?>
										<input type="hidden" name="action" value="sajtvakt_admin">
										<input type="hidden" name="do" value="rollback">
										<input type="hidden" name="snapshot" value="<?php echo esc_attr( $s['id'] ); ?>">
										<button class="button" type="submit">Återställ</button>
									</form>
									<?php endif; ?>
								</td>
							</tr>
						<?php endforeach; ?>
						</tbody>
					</table>
				<?php endif; ?>
			</div>
		</div>
		<style>
			.sajtvakt .sv-card{background:#fff;border:1px solid #dcdcde;border-radius:6px;padding:4px 20px 16px;margin:16px 0;max-width:900px}
			.sajtvakt .sv-key{display:flex;gap:8px;align-items:center}
			.sajtvakt .sv-key input{flex:1;min-width:0}
			.sajtvakt .sv-table th{width:260px}
			.sajtvakt .sv-right{text-align:right;white-space:nowrap}
			.sajtvakt .sv-danger{color:#b32d2e}
		</style>
		<script>
			(function(){
				var i=document.getElementById('sv-conn'),s=document.getElementById('sv-show'),c=document.getElementById('sv-copy');
				s.addEventListener('click',function(){var h=i.type==='password';i.type=h?'text':'password';s.textContent=h?'Dölj':'Visa';});
				c.addEventListener('click',function(){
					var done=function(){c.textContent='Kopierad!';setTimeout(function(){c.textContent='Kopiera';},1800);};
					if(navigator.clipboard){navigator.clipboard.writeText(i.value).then(done);}else{i.type='text';i.select();document.execCommand('copy');i.type='password';done();}
				});
			})();
		</script>
		<?php
	}
}
