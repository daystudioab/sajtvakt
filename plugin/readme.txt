=== Sajtvakt ===
Requires at least: 6.0
Requires PHP: 7.4
Stable tag: 0.3.0
License: GPLv2 or later

Backup, uppdateringar och återställning för WordPress – styrs från din egen Sajtvakt-server.

== Installation ==

1. Tillägg → Lägg till nytt → Ladda upp tillägg → välj sajtvakt.zip → Aktivera.
2. Gå till Verktyg → Sajtvakt och kopiera anslutningsnyckeln.
3. Klistra in nyckeln när du lägger till sajten i Sajtvakt-servern.

== Vad pluginet gör ==

* Backup i små steg (fungerar även på billiga webbhotell med kort körtid):
  databasen plus wp-content, wp-config.php och .htaccess. Cachemappar och
  andra backupplugins mappar hoppas över.
* Uppdaterar tillägg, teman, översättningar och WordPress. Före varje
  uppdatering sparas en kopia av det som uppdateras.
* Återställer till läget före en uppdatering med ett klick – även om
  uppdateringen kraschat sajten (via nödingången rescue.php, som fungerar
  utan att WordPress startar).
* Tar emot en backup ("push"): återställ samma sajt, eller flytta/klona en
  sajt hit med automatiskt adressbyte (även i Elementor-data och serialiserad
  data). Allt förbereds vid sidan av och byts in först när allt är klart.

* Uppdaterar sig själv: nya versioner hämtas från github.com/daystudioab/sajtvakt
  och installeras av Sajtvakt-servern på natten som vilket tillägg som helst.

== Säkerhet ==

* Varje anrop är signerat med sajtens nyckel (HMAC-SHA256), tidsstämplat och
  kan inte spelas upp igen.
* Nyckeln och arbetsfilerna ligger i wp-content/sajtvakt-data, skyddade mot
  direkt åtkomst.
* Skapa en ny nyckel under Verktyg → Sajtvakt om den kan ha läckt.

== Bra att veta ==

* wp-config.php skrivs aldrig över vid återställning – sajtens egna
  databasuppgifter behålls.
* Kräver att WordPress kan skriva filer direkt (standard hos de flesta
  webbhotell). Annars fungerar backup, men inte uppdateringar.
* Multisite stöds inte ännu.
* Avinstallation tar bort alla Sajtvakts filer och inställningar.
