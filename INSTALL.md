# Installing ByStack

ByStack has two parts:

- **The Controller** — you install this once. It runs the dashboard.
- **An Agent** — a tiny helper you add to each server you want to manage.

Install the Controller, open the dashboard, then add your servers one by one.

**Already running it?** You want [Upgrading](#upgrading) — the Controller and
every agent, in that order.

Everything below is meant to be pasted as-is.

---

## Step 1 · Install the Controller

Once, on the machine you want as your control center. There are two ways, and
they are the same software — pick by what the machine already has.

### One command (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-controller.sh \
  | sudo sh
```

It needs `systemd` and a `python3.12`, and it checks for both before it
downloads anything. It fetches two signed binaries, verifies them against a key
that is not on the machine serving them, creates the account and the directory
they live in, writes `/etc/bystack/bystack.yaml`, installs four units and
starts the Controller. It prints the dashboard's address when it is answering.

**This is the install that can update itself.** The Controller it puts down is
one file, so a new release is a rename with the old one kept beside it — which
is what makes the **Update system** button in the dashboard able to promise a
rollback rather than attempt one. No other install path has that
([ADR-0018](docs/adr/0018-controller-self-update.md)).

Before piping anything into `sudo sh`, it is fair to look first:

```bash
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-controller.sh | less
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-controller.sh | sh -s -- --keys
```

The second prints the release signing keys it will accept a release from, and
nothing else. Those are the same keys compiled into the updater it installs, so
they are what this machine will keep accepting for every update after this one.

Useful flags: `--bind 0.0.0.0` and `--port`, `--agents` with `--server-name` to
open the fleet listener during the install rather than afterwards, `--binary`
and `--manager` to install files you copied over yourself, and `--uninstall`.

### Or with Docker

If the machine already runs containers and you would rather not add a Python to
it. The only thing you have to supply is a working Docker:

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

Then, paste all four lines:

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

**An image is upgraded by pulling a new one**, so this install has no **Update
system** button and the dashboard says so rather than showing one that could
not work. That is the trade: less to install, one more ssh per release.

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
ask for it, because it is a second port on a real network. Two things change
under `agents:`, and they are one decision:

```yaml
agents:
  enabled: true
  server_names: ["localhost", "127.0.0.1", "::1", "10.0.0.5"]
```

Put the address your *other* servers will use to reach this one in
`server_names` — the IP or hostname, exactly as they will type it. The listener
issues itself a certificate for those names, and an agent that dialled a name
that is not in the list will refuse to connect and tell you so.

**On the one-command install**, the file is `/etc/bystack/bystack.yaml`:

```bash
sudo nano /etc/bystack/bystack.yaml
sudo systemctl restart bystack-controller
```

(`install-controller.sh --agents --server-name 10.0.0.5` does the same thing at
install time, and is the shorter path if you know the address up front.)

**On the Docker install**, it is `packaging/bystack.container.yaml` in the
clone:

```bash
docker compose up -d
```

### 3b. For each server

Click **Add host** in the dashboard. There are two ways in, side by side, and
neither is the fallback — pick by whether this Controller can reach that
machine on SSH.

#### Install it from here

Paste the addresses, give it a root login, press **Install the agent**.

The Controller logs in over SSH, uploads the agent and the installer, runs it,
waits for the machine to dial back, and moves to the next one. One host at a
time, and it stops if one fails — the same staged shape as the fleet upgrade,
for the same reason.

Three things about it are worth knowing before you type a password into a
browser:

- **Nothing is kept.** The credential is used to open one connection and is
  never written to disk, never logged, and never returned by any route. There
  is no host list, no stored key, and no "reconnect" — which is deliberate,
  because a Controller that could reach your fleet at will is the thing this
  product's whole shape exists to avoid
  ([ADR-0019](docs/adr/0019-agent-deployment-over-ssh.md)).
- **That machine needs no internet.** The agent and the installer are uploaded
  over the same connection. Only a host whose architecture this Controller has
  no agent for fetches one from GitHub, and the dialog says so when it happens.
- **Reach the dashboard over `ssh -L`.** Nothing here asks a browser for a
  password, so a dashboard published on your network is one anybody on it can
  post a root credential to. The dialog warns you when it is not on loopback.

The account has to be `root`, or one that can `sudo` without a password. A
machine that already runs an agent is reported as *already managed* and is left
alone.

The first connection to an address records its host key and shows you the
fingerprint — compare it to what `ssh-keygen -l -f /etc/ssh/ssh_host_ed25519_key.pub`
prints on that machine. If a later deployment sees a different key, it refuses
and prints both, rather than asking you a question you would answer yes to.

#### Or copy a command

Which is the answer for a host the Controller cannot reach: behind NAT, on a
laptop, on a network with no inbound SSH.

1. Switch to **Copy a command**.
2. Copy the line it shows — it already has your one-time token in it.
3. Paste and run it **on the server you are adding**, as root.

It looks like this:

```bash
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-agent.sh \
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
| `no python3.12 on this host` | The one-command install ships the Controller as a single file, which is built for one CPython. Install it, or use Docker. |
| `bystack-controller` restarts every five seconds | `journalctl -u bystack-controller -n 50`. A missing or unparseable `/etc/bystack/bystack.yaml` is the usual answer. |
| No **Update system** button on the panel | You are on Docker, a checkout or a wheel. The panel says which. Only the single-file install has one file to replace. |
| The map is completely empty | The Docker socket group. See below. |
| The pasted `curl` in Step 3 returns 404 | No release has been published for this Controller's version yet. Tag one (`git tag v0.5.0 && git push origin v0.5.0`) or use the `--binary` form above. |
| Agent says the certificate name does not match | The address it dialled is not in `server_names`. Step 3a. |

**The empty map is worth its own paragraph**, because nothing else reports it.
The Controller stays healthy and the dashboard loads normally — it *is* fine;
it just has no agent. Ask it directly:

```bash
curl -s localhost:8000/api/v1/healthz
```

`"providers":[]` with `"node_count":0` means the bundled agent could not read
the engine's socket, which is the group. On Docker, fix it and restart:

```bash
echo "DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)" > .env
docker compose up -d
```

On the one-command install, the account is `bystack` and the fix is to put it
in the group that owns the socket:

```bash
sudo usermod -aG "$(stat -Lc '%G' /var/run/docker.sock)" bystack
sudo systemctl restart bystack-controller
```

A working Controller answers that same URL with `"state":"ready"` and a node
count in the dozens.

Logs, always:

```bash
docker compose logs -f controller        # Docker
journalctl -u bystack-controller -f      # one-command install
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

Step 1 has the two that are meant for a machine you intend to keep. Two more
exist and are the same software:

**By hand, from the release files**, which is what `install-controller.sh` does
and is worth having written down — for an air-gapped host, for a
configuration-management module, or to see what a one-line installer actually
put on your machine:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin bystack
sudo install -D -m 0755 bystack-controller-$(uname -m) /opt/bystack/bin/bystack-controller
sudo install -D -m 0755 bystack-manager-$(uname -m)    /opt/bystack/bin/bystack-manager
sudo install -d -m 1770 -o root   -g bystack /opt/bystack/ipc
sudo install -d -m 0700 -o root   -g root    /opt/bystack/state
sudo install -d -m 0755 -o root   -g root    /opt/bystack/releases
sudo install -d -m 0755 -o bystack -g bystack /opt/bystack/cache
sudo install -m 0644 packaging/systemd/bystack-controller.service /etc/systemd/system/
sudo install -m 0644 packaging/systemd/bystack-manager.path /etc/systemd/system/
sudo install -m 0644 packaging/systemd/bystack-manager.service /etc/systemd/system/
sudo install -m 0644 packaging/systemd/bystack-manager-rollback.service /etc/systemd/system/
sudo systemctl enable --now bystack-controller bystack-manager.path bystack-manager-rollback
```

You also need `/etc/bystack/bystack.yaml`, and two settings in it are not
preferences — the update button works without them and its second half does
not:

```yaml
agents:
  state_dir: /var/lib/bystack
  releases_dir: /opt/bystack/releases    # where the updater leaves the fleet's agents
  manager_ipc_dir: /opt/bystack/ipc      # the two files the update travels through
```

`releases_dir` is the one that fails quietly: point it elsewhere and the
Controller updates itself, reports success, and never offers the fleet the
release that was downloaded for it. `backend/bystack.example.yaml` documents
everything else.

The `1770` on `ipc/` is load-bearing rather than decoration. The Controller
runs as `bystack` and creates its update request in there; the sticky bit is
what stops it also being able to delete root's report of how the update went.

**A wheel**, which is what a developer checkout uses. It carries the agent
binary and the dashboard, and it is upgraded the way it was installed:

```bash
python3 -m venv /opt/bystack-venv
/opt/bystack-venv/bin/pip install bystack-*.whl      # from a release
/opt/bystack-venv/bin/bystack
```

**Systemd**, from the units in `packaging/systemd/`.

Building from a checkout without Docker is in the [README](README.md).

---

## Upgrading

Two steps, in this order: the Controller, then each server. What is in each
release is [CHANGELOG.md](CHANGELOG.md).

On a single-file install both steps are one button — see [If you installed the
single file](#if-you-installed-the-single-file) below.

**The order is not arbitrary.** An agent asks the Controller what it can do,
not the other way round, so a new Controller with old agents is a working
fleet with fewer features on the hosts you have not got to yet. The reverse —
new agents, old Controller — is not tested and is not the path.

### The Controller

**If you used the one-command install, this is a button** — see [If you
installed the single file](#if-you-installed-the-single-file) below. The rest
of this section is the Docker install.

From the clone, on the machine it runs on:

```bash
cd bystack
git pull
docker compose up -d --build
```

**`--build` is load-bearing.** `compose.yaml` names both a published image and
a way to build one, so without it `docker compose up -d` finds the `:latest`
image already on this machine, uses it, and reports success — you get `Started`
and the old software. (`docker compose up -d --pull always` is the other
correct answer, if you would rather take the published image than build.)

Your data is a named volume and is not touched: the CA, the enrolment
registry, the audit log and your watch lists all survive. `.env` is not tracked
by git, so `DOCKER_GID` and `BYSTACK_BIND` survive too.

One thing does collide. If you turned on the fleet listener in Step 3a, you
edited `packaging/bystack.container.yaml`, which *is* tracked — so `git pull`
can stop with `Your local changes to the following files would be overwritten`.
Keep your version and take the new one's additions by hand:

```bash
git stash                       # your edit, set aside
git pull
git stash pop                   # re-apply it; resolve if it conflicts
docker compose up -d --build
```

Then confirm you are running what you think you are:

```bash
curl -s localhost:8000/api/v1/healthz
```

The `version` in that answer is the Controller's, and it is the same string the
**Add host** command points at.

### Each server

**If the Controller has a signed release for them, this is a button.** Open
**Hosts**: when there is one to push and hosts that can take it, the panel says
so and offers *Upgrade them to ‹version›*. It goes one host at a time, waits
for each to come back on the new version before touching the next, and stops if
one does not. `bystack-ctl upgrade --watch` is the same thing without a
browser. Nothing has to be installed on the hosts, nothing has to reach GitHub,
and no command has to be pasted anywhere.

Two things about it are worth knowing before you press it:

- **The Controller cannot forge a release.** It holds no signing key. Each host
  checks the release against a key compiled into its own agent, so a Controller
  someone else has taken over can withhold an upgrade or send an old one — and
  cannot make a host run anything.
- **A host that comes up unable to reach the Controller undoes itself.** The
  new agent is on probation until it connects and is admitted; if that has not
  happened ten minutes after the swap, the machine puts its previous binary
  back and restarts it, without anybody being told to go and look.

Setting that up is [CHANGELOG.md](CHANGELOG.md) under the release that
introduced it: mint a signing key, build and sign an agent, and drop the three
files in the Controller's `releases` directory. Until you do, the Controller
holds nothing to push and the paragraphs below are the path — which is also
what happens on any host running an agent from before this existed.

#### By hand, which is still the first upgrade on every host

You do not have to compose this yourself. Once the Controller is upgraded,
**Hosts** names the hosts that are behind and shows the exact line to run, and
so does `bystack-ctl hosts`. It is the command that installed the agent, with
the new tag in the URL and **no `--token`**:

```bash
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-agent.sh \
  | sudo sh -s -- --controller wss://10.0.0.5:8443
```

Leaving the token out is what makes this an upgrade instead of a second host.
The certificate under `/var/lib/bystack-agent` is this machine's identity to
the Controller and stays where it is; a token sitting beside a stored
certificate makes the agent enrol again and appear as a stranger waiting for
your approval, while the host you actually have goes quiet.

You do not need to stop anything first. The new binary replaces the old file
rather than being written through it, so the running agent finishes on the one
it started with and the change takes effect at the restart the script does at
the end. The host drops off the map for a second or two and comes back.

Flags you passed the first time — `--read-only`, `--socket` — are kept without
being repeated, and the script prints which ones it carried over. Passing one
again still wins, which is how you change your mind.

**On a host with no route to GitHub**, the offline form from Step 3b upgrades
just as well: build or download the binary once, copy it over, and pass
`--binary` with no token.

Afterwards, **Hosts** shows each agent's version, so a host you missed is
visible rather than merely quiet — and a host you have run this on once can be
upgraded from the panel next time.

### If you installed the single file

Press **Update system** on the Hosts panel, and watch it. There is nothing else
to do, and the two steps above happen in that order by themselves:

1. The Controller is stopped, replaced and started. The dashboard goes quiet
   for a few seconds in the middle of this — that is the swap, not a fault, and
   the page says so. If the new Controller does not answer within three
   minutes, the previous one is put back automatically and the panel tells you
   the release does not run on this machine.
2. The signed agents for the new version are downloaded, and the fleet is
   rolled forward one host at a time — the same staged rollout as above, with
   each host confirmed before the next is touched.

From a terminal, the same thing:

```bash
sudo /opt/bystack/bin/bystack-manager request     # or `request 0.5.0`
/opt/bystack/bin/bystack-manager status
```

Nothing is installed that is not signed by the key this Controller was built to
trust, and the version has to be higher than the one on the disk. Downgrading
is not an operation; rollback is, and it is automatic.

### If you installed the wheel instead

```bash
/opt/bystack-venv/bin/pip install --upgrade bystack-0.5.0-*.whl   # from the release
sudo systemctl restart bystack-controller                          # if it runs as a unit
```

The wheel carries the matching agent binary, so a Controller upgraded this way
also hands out the right version to new hosts. There is no **Update system**
button on this install — there is no single file to replace — and the dashboard
says so rather than offering one that cannot work.

---

## Removing things

One server, run on that server. The installer was piped from a URL rather than
left on the host, so fetch it the same way — `--uninstall` needs neither a
controller nor a token:

```bash
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-agent.sh \
  | sudo sh -s -- --uninstall
```

(`sudo sh install-agent.sh --uninstall` if you do have the file on that host.)
Either form keeps `/var/lib/bystack-agent` — the certificate in there is what
this machine is to the Controller, and removing it turns "I reinstalled the
agent" into "a new host appeared and the old one went quiet". Delete it by hand
if that is what you mean.

The Controller. If you used the one-command install:

```bash
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-controller.sh \
  | sudo sh -s -- --uninstall
```

That stops everything, removes the units and deletes `/opt/bystack` whole —
which is the point of everything living in one directory. It keeps
`/var/lib/bystack`, for the same reason `--uninstall` on a host keeps its
certificate: the CA private key in there is what every enrolled agent's
certificate chains to, so deleting it does not uninstall a Controller, it
re-enrols a fleet. `/etc/bystack/bystack.yaml` is kept too, so a reinstall
comes back with the same settings.

From the Docker install, from the clone:

```bash
docker compose down            # keeps your CA and audit log
docker compose down -v         # deletes them too
```

---

**Questions?** See the full [README](README.md).
