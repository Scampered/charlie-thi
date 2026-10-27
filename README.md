# Charlie — run it on your own computer

Charlie runs in two Docker containers: **nginx6** (serves the chat page) and
**flask6** (Python: Flask, ChatterBot, Scrapy). You only need Docker.

## 1. Install

- **Docker Desktop** — https://www.docker.com/products/docker-desktop/
  (Windows: let it install/enable WSL 2 when it asks, then restart the PC.)
- Start Docker Desktop and wait until it says *Engine running*.

Nothing else: Python, ChatterBot, Scrapy and nginx are all installed *inside* the containers.

## 2. Start Charlie

1. Unzip `cai-bot.zip` somewhere, e.g. `Documents\cai-bot`.
2. Open a terminal **in that folder**
   - Windows: open the folder in Explorer, click the address bar, type `cmd`, press Enter
   - Mac: right-click the folder → *New Terminal at Folder*
3. Run:

   ```
   docker compose up -d --build
   ```

   The first time takes 5–10 minutes (it downloads Python 3.7 and installs everything).
   After that it starts in seconds.
4. Open **http://localhost:5002** in your browser.
5. Say "hi". The first reply takes a few seconds (Charlie teaches himself small talk once).

## 3. Open it from your phone or another computer (same Wi-Fi)

1. Find your computer's local IP address:
   - Windows: `ipconfig` → "IPv4 Address", e.g. `192.168.0.23`
   - Mac: `ipconfig getifaddr en0`
   - Linux: `hostname -I`
2. On the other device open `http://192.168.0.23:5002` (your IP, port 5002).
3. Windows may ask whether to allow Docker through the firewall — click **Allow** for private networks.

No IP address has to be changed in any file: the page always talks to the address it was opened from.

## 4. Everyday commands (run in the cai-bot folder)

| Command | What it does |
|---|---|
| `docker compose up -d` | start Charlie |
| `docker compose down` | stop Charlie |
| `docker compose restart flask6` | after changing a `.py` file |
| `docker compose up -d --build` | after changing `web/charlie.html`, `web/logic.js`, `requirements.txt` or a `Dockerfile` (then Ctrl+F5 in the browser) |
| `docker compose logs flask6 --tail 30` | show errors from the Python side |
| `docker compose ps` | are both containers running? |

Docker Desktop also starts Charlie again automatically after a restart of the computer
(as long as you didn't stop him with `docker compose down`).

## 5. Charlie's knowledge

- `sentences.json` already contains the Ingolstadt and THI pages, so Charlie works immediately.
- **Add pages:** paste Wikipedia links (separated by spaces) into the field in the top right → *Scrape*.
- **Clear memory:** *Clear memory* empties `sentences.json` (a copy is kept as `sentences.backup.json`).
- **Start over with fresh pages:** *Clear memory*, then scrape
  `https://en.wikipedia.org/wiki/Ingolstadt https://en.wikipedia.org/wiki/Technische_Hochschule_Ingolstadt`
- Scraping by hand in the terminal:

  ```
  docker compose exec flask6 python -m scrapy runspider /app/caifirstbot.py -a url="https://en.wikipedia.org/wiki/Bavaria" -O /app/data.json
  docker compose exec flask6 python /app/prepare_data.py /app/data.json
  ```

- Chat with Charlie in the terminal (shows the winning sentence and its links):

  ```
  docker compose exec -w /app flask6 python charlie.py
  ```

## 6. Files

```
cai-bot/
├── docker-compose.yml   starts both containers
├── Dockerfile           the Python container (Python 3.7)
├── requirements.txt     Python packages
├── create_app.py        Flask routes: /chat, /train (+ /scrape, /clear-memory from scrape_api.py)
├── charlie.py           the answer engine
├── prepare_data.py      data.json -> sentences.json (cleaning, categories, links)
├── scrape_api.py        Scrape and Clear memory buttons
├── caifirstbot.py       the Wikipedia spider
├── sentences.json       Charlie's knowledge
└── web/
    ├── charlie.html     the page (put your names in "Project made by ...")
    ├── logic.js         sends your messages to the server
    ├── site.conf        nginx: serves the page, forwards /api/... to Flask
    └── Dockerfile       the web container
```

Created automatically: `smalltalk.db` (ChatterBot's greetings), `data.json` (last scrape),
`sentences.backup.json` (before the last Clear memory).

## 7. Problems

| Problem | Fix |
|---|---|
| `docker: command not found` / cannot connect to the Docker daemon | Start Docker Desktop and wait for *Engine running*. |
| `port is already allocated` (5002) | Change `"5002:80"` in docker-compose.yml to e.g. `"5050:80"` and open http://localhost:5050 |
| Page shows, but every answer is "Sorry" | `docker compose logs flask6 --tail 30`; check that `sentences.json` is in the folder. |
| Error alert "server not available" | `docker compose ps` — is flask6 running? If not: `docker compose logs flask6`. |
| Changes to the page don't show | `docker compose up -d --build`, then Ctrl+F5. |
| Scrape says "could not run" | the reason is printed under the field; more in `docker compose logs flask6`. |
