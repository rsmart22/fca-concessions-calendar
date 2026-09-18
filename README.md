# MaxPreps -> Concessions calendar

1. Test locally first (from home, not the cloud):
   `pip install requests beautifulsoup4 && python maxpreps_sync.py --dry-run`
   Compare the printed list to the MaxPreps pages. If it matches, continue.
2. Create a PUBLIC GitHub repo, push these files, run `python maxpreps_sync.py` once and commit
   `docs/concessions.ics` + `state.json`.
3. Repo Settings -> Pages -> deploy from branch `main`, folder `/docs`.
   Feed URL: `https://<user>.github.io/<repo>/concessions.ics`
4. Actions tab -> run "Sync MaxPreps schedule" manually once. If it fails with a 403/0 games,
   MaxPreps is blocking GitHub's servers: run the script from the home lab on cron instead and
   `git push` the results.
5. Optional alerts: add repo secret `NOTIFY_WEBHOOK` = a Home Assistant webhook URL whose
   automation sends `{{ trigger.json.title }}` / `{{ trigger.json.message }}` to both phones.
6. Subscribe on each phone: Settings -> Apps -> Calendar -> Calendar Accounts -> Add Account ->
   Other -> Add Subscribed Calendar -> paste the feed URL.

Add a sport/team: add a line to TEAMS in the script. Change arrival lead: LEAD_MINUTES.
