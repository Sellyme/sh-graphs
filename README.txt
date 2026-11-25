To sync any changes to docker:

docker compose up -d --build

To import data from an SH users list:

Go to users tab, click ? button, copy the code snippet shown there, and paste it into the console on any SH users page. Then type `user_data` into console, copy object, and paste into the site's Import button.

EHP auto-scan (runs inside server.py): top 25 users by EHP are rescanned after 24 hours, ranks 26-50 after 7 days, 51-100 after 30 days, one scan at a time and at least 5 minutes apart. Failures back off from 30 minutes up to 12 hours. Set SH_AUTOSCAN=0 to turn it off. History is stored in sh-graphs-ehp-history.jsonl: whenever a scan lands more than 20 hours after a user's previous snapshot, the appids they completed are saved (first snapshot in full, later ones as added/removed deltas). Historical EHP, efficiency etc. are recomputed from the current game data, so they respect the filters and improve as medians improve.
History before a user's first snapshot is estimated from each game's last unlock date (shown as a line without markers). Games saved before dates were stored get dates the next time that user is scanned.