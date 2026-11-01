# Sajtvakt

Egen ersättare för ManageWP: nattlig backup, uppdateringar med automatisk återställning,
upptidskoll, skärmdumpar och flytt av sajter – för WordPress-sajter, styrt från en egen server.

| Mapp | Vad |
|---|---|
| `plugin/` | WordPress-tillägget som installeras på varje sajt |
| `server/` | Panelen och schemaläggaren (Docker), körs på sajtvakt.se – installationsguide i [server/README.md](server/README.md) |
| `dist/` | Färdigt tillägg (`sajtvakt.zip`) och `update.json` som sajterna läser för att hitta nya versioner |
| `tools/release.py` | Bygger `dist/` inför en ny version av tillägget |

## Uppdatera servern

Från datorn:

```bash
ssh root@65.108.253.83 "cd /opt/sajtvakt/server && git pull && docker compose up -d --build"
```

Inställningar (`server/.env`) och all data (`server/data/`) ligger bara på servern och påverkas inte.

## Ny version av tillägget

1. Ändra i `plugin/` och höj versionen i `plugin/sajtvakt.php` **och** `plugin/includes/class-core.php`.
2. `python3 tools/release.py "Vad som är nytt"`
3. `git commit -am "Sajtvakt X.Y.Z" && git tag vX.Y.Z && git push && git push --tags`

Sajterna ser den nya versionen inom sex timmar (direkt när panelen hämtar status), och
Sajtvakt-servern installerar den på natten – med ögonblicksbild och återställning som för
alla andra tillägg. Tillägget hämtar bara paket från det här repot.
