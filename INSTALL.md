# Installing ByStack

ByStack has two parts:

- **The Controller** — you install this once. It runs the dashboard.
- **An Agent** — a tiny helper you add to each server you want to manage.

Install the Controller, open the dashboard, then add your servers one by one.

Everything below is meant to be pasted as-is. The only thing you have to
supply is a machine with Docker on it:

```bash
docker ps
```

If that prints a table — even an empty one — you are ready. If it says
`permission denied while trying to connect to the docker API`, add yourself to
the `docker` group and start a new login shell:

```bash
sudo usermod -aG docker $USER    # then log out and back in
```

Or put `sudo` in front of every `docker` command below. (`docker version` is
not the check: it prints the client's version happily while the daemon is
unreachable.)

---

## Step 1 · Install the Controller

Once, on the machine you want as your control center. Paste all four lines:

```bash
git clone https://github.com/yaliby/bystack.git
cd bystack
echo "DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)" > .env
docker compose up -d --build
```

The `cd bystack` is not decoration: `docker compose` reads `compose.yaml` out
of the directory you are standing in, so the last two lines only work from
inside the clone. `no such file or directory` means you are somewhere else.

The third line tells the container which group owns your Docker socket, which
differs between distributions. Without it the Controller still starts — it just
comes up with an empty map.

The fourth line builds the image from the source you just cloned. **The first
build takes a few minutes** (it compiles the agent and the dashboard) and
prints a lot. Later runs reuse the cache and take seconds.

When it finishes:

```bash
docker compose ps
```

`controller` should be `running` and, after about fifteen seconds, `healthy`.

---

## Step 2 · Open the dashboard

### If you installed on the machine in front of you

Open <http://127.0.0.1:8000>.

### If you installed on a server you reach over SSH

The dashboard is bound to loopback on purpose — nothing asks a browser for a
password, so the Controller does not put itself on your network without being
told to. Two ways to reach it:

**Forward the port** (nothing changes on the server, works over any SSH). Run
this on *your* machine, not on the server:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 8080:127.0.0.1:8000 you@your-server
```

Leave it running and open <http://127.0.0.1:8080>.

The two numbers are two different machines. `8080` is a port on your own
machine and you can pick any free one; `127.0.0.1:8000` is what the Controller
listens on over there and does not change. 8080 rather than 8000 on purpose —
if you also have a Controller running locally, it already holds 8000, and
`bind [127.0.0.1]:8000: Address already in use` is that collision on *your*
side, not a problem with the server.

`ExitOnForwardFailure=yes` is not optional dressing. Without it, ssh that
cannot bind IPv4 will happily bind `[::1]` alone and keep running, leaving
`http://localhost:8000` pointing at either machine depending on which address
your browser resolves first — a much worse afternoon than an error message.

And `you@your-server` is a hostname, not a URL: `yali@192.168.1.224`, never
`yali@http://192.168.1.224/`.

**Or publish it**, if you trust every machine that can reach that server:

```bash
echo "BYSTACK_BIND=0.0.0.0" >> .env
docker compose up -d
```

Then open `http://<server-ip>:8000`. Do this only on a private network, or put
your own login page in front of it.

Either way, you should see the Controller's own containers on the map within a
second or two. It manages itself automatically — you never add it as a server.

---

## Step 3 · Add your other servers

Skip this if you only have one machine.

### 3a. Turn on the fleet listener, once

Agents dial *in* to the Controller on port 8443. That listener is off until you
ask for it, because it is a second port on a real network. Open
`packaging/bystack.container.yaml` and change two things under `agents:`:

```yaml
agents:
  enabled: true
  server_names: ["localhost", "127.0.0.1", "::1", "10.0.0.5"]
```

Put the address your *other* servers will use to reach this one in
`server_names` — the IP or hostname, exactly as they will type it. The listener
issues itself a certificate for those names, and an agent that dialled a name
that is not in the list will refuse to connect and tell you so.

Then:

```bash
docker compose up -d
```

### 3b. For each server, one command

1. In the dashboard, click **Add host**.
2. Copy the command it shows you — it already has your one-time token in it.
3. Paste and run it **on the server you are adding**, as root.

It looks like this:

```bash
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.1.0/scripts/install-agent.sh \
  | sudo sh -s -- --controller wss://10.0.0.5:8443 --token bst1.…
```

It fetches one static binary, checks it against the release checksums, writes a
systemd unit and starts it. The server needs outbound access to the Controller
and to GitHub, and nothing needs to be opened *on* it.

The new server appears on the map within a second or two. If it shows as
**pending**, click **Approve** — that is the dashboard asking "is this really
your server?".

**No network access to GitHub on that server?** Get the agent binary once — off
a release, or by building it from the clone with
`./scripts/build-agent.sh --target x86_64-unknown-linux-musl`, which leaves it
in `dist/` — then copy both files over and skip the download entirely:

```bash
scp scripts/install-agent.sh dist/bystack-agent-x86_64 you@newserver:/tmp/
ssh you@newserver 'sudo sh /tmp/install-agent.sh --binary /tmp/bystack-agent-x86_64 \
  --controller wss://10.0.0.5:8443 --token bst1.…'
```

---

## If something looks wrong

| What you see | What it is |
| --- | --- |
| `open .../compose.yaml: no such file or directory` | You are not in the `bystack` directory. `cd` into the clone. |
| `port is already allocated` | Something already holds 8000 or 8443 on this machine. `ss -ltnp \| grep -E ':8000\|:8443'`. |
| `bind [127.0.0.1]:8000: Address already in use` | From `ssh -L`, and it is about *your* machine, not the server. Pick another local port: `-L 8080:127.0.0.1:8000`. |
| `ssh: Could not resolve hostname http://…` | `ssh` takes `user@host`, not a URL. Drop the `http://` and the trailing slash. |
| Browser cannot connect at all | The port is on loopback. See Step 2. |
| The map is completely empty | The Docker socket group. See below. |
| The pasted `curl` in Step 3 returns 404 | No release has been published for this Controller's version yet. Tag one (`git tag v0.1.0 && git push origin v0.1.0`) or use the `--binary` form above. |
| Agent says the certificate name does not match | The address it dialled is not in `server_names`. Step 3a. |

**The empty map is worth its own paragraph**, because nothing else reports it.
The container stays `healthy` and the dashboard loads normally — the Controller
*is* fine; it just has no agent. Ask it directly:

```bash
curl -s localhost:8000/api/v1/healthz
```

`"providers":[]` with `"node_count":0` means the bundled agent could not read
the Docker socket, which is the group. Fix it and restart:

```bash
echo "DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)" > .env
docker compose up -d
```

A working Controller answers that same URL with `"state":"ready"` and a node
count in the dozens.

Logs, always:

```bash
docker compose logs -f controller
```

---

## Good to know

- **Your servers stay closed.** Each server reaches *out* to the Controller;
  you do not open any ports on them.
- **Nothing gets deleted.** ByStack can start, stop and restart containers —
  it cannot remove or destroy anything.
- **No login to set up.** ByStack is built for one trusted network you own.
  Keep the dashboard on a private network, or behind your own login page.
- **One directory is worth backing up:** the `bystack-state` volume. It holds
  the CA key, the enrolment registry, the audit log and your watch lists.
  Losing it means re-enrolling every host, and re-choosing which services you
  were watching.

---

## Watching services and processes, not just containers

Open **Hosts**, pick a server, and press **Services & processes**. You get a
list of what that machine actually has — systemd units and running processes —
and whatever you select is drawn on the map beside its containers, with the
same start / stop / restart buttons.

You can also type a name that is not there yet. A service you are about to
install shows as *not installed* until it appears, which is the point: a watch
that silently showed nothing would look exactly like one you forgot to add.

**Watching works with no extra setup.** Operating needs one or two files,
depending on what you want, and both are in
[`packaging/`](packaging/) with the reasoning written on them:

| Want to | Install | Grants |
|---|---|---|
| Start / stop / restart services | `polkit/49-bystack-agent.rules` into `/etc/polkit-1/rules.d/` | The agent may manage units on that host |
| Watch and signal processes | `systemd/bystack-agent-host.conf` into `/etc/systemd/system/bystack-agent.service.d/` | The agent may see other users' processes, and signal them |

Without them nothing breaks and nothing is hidden: services still appear with
their live state, and pressing a button reports systemd's own refusal rather
than failing quietly. [ADR-0016](docs/adr/0016-watched-units-and-processes.md)
is why the grants are separate, opt-in, and bounded by the list you chose.

---

## Other ways to install the Controller

The compose file above is the shortest path. Two others exist and are the same
software:

**A wheel**, if you would rather not run the Controller in a container. It
carries the agent binary and the dashboard, so nothing else is needed:

```bash
python3 -m venv /opt/bystack
/opt/bystack/bin/pip install bystack-*.whl      # from a release
/opt/bystack/bin/bystack
```

**Systemd**, from the units in `packaging/systemd/`.

Building from a checkout without Docker is in the [README](README.md).

---

## Removing things

One server, run on that server:

```bash
sudo sh install-agent.sh --uninstall
```

The Controller, from the clone:

```bash
docker compose down            # keeps your CA and audit log
docker compose down -v         # deletes them too
```

---

**Questions?** See the full [README](README.md).
