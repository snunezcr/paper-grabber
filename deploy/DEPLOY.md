# Deploying Research Stream

An always-on deployment for one person, reached over Tailscale. About half an
hour to set up. The default here is **Google Compute Engine**; a Hetzner
alternative follows.

The shape of it:

```
tablet / laptop ──(Tailscale, HTTPS)──▶ VM ──▶ 127.0.0.1:8823
```

The app binds **loopback only** and is never exposed to the public internet.
Tailscale is what makes it reachable, and only from your own devices. This
matters: the web API has no authentication, so a public bind would hand Drive
browsing and filing to anyone who found the port.

## Which Google Cloud — a VM, not Cloud Run

Use **Compute Engine** (a plain VM). It runs everything below unchanged.

Do **not** use Cloud Run or App Engine. This app keeps its state in a local
SQLite file, stages downloaded PDFs on local disk, and runs its refresh and
upload jobs in-process. A serverless runtime resets that filesystem on every
deploy and may run several instances that cannot see each other's state — so it
would need Cloud SQL, a storage bucket, and an external job queue bolted on,
for one user. A single small VM is cheaper and simpler.

**Machine type.** An **e2-small** (2 vCPU, 2 GB) is the comfortable choice: a
PDF download is buffered in memory up to a 200 MB cap, and 2 GB carries that
with room for the OS. It runs about $13/month plus a little for disk.

**Free tier.** One **e2-micro** in `us-west1`, `us-central1`, or `us-east1` is
free every month. It has only 1 GB of RAM. The PDF download cap defaults to
100 MB (`PG_MAX_PDF_MB`), which keeps peak memory well under that ceiling and
still covers every real paper — the largest seen in practice is ~30 MB. Pick
the e2-micro to pay nothing, the e2-small only if you want the headroom.

## 1. Create the server

Console → **Compute Engine → VM instances → Create**, or:

```bash
gcloud compute instances create research-stream \
  --machine-type=e2-small \
  --zone=us-east1-b \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB
```

For the free tier, use `--machine-type=e2-micro` in a free-tier zone
(`us-west1-b`, `us-central1-a`, `us-east1-b`).

Leave the firewall alone: Google Cloud opens nothing inbound by default except
SSH, and **nothing else should be opened** — port 8823 is reached through
Tailscale, not the public interface.

```bash
gcloud compute ssh research-stream --zone=us-east1-b
```

`gcloud` provisions your login as a sudo-capable user already, so there is no
root account to migrate away from.

## 2. Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

Follow the printed URL to authorise the machine into your tailnet. From now on
you can reach it as `research-stream` (its `*.ts.net` name) from any of your
devices, and `tailscale ssh research-stream` needs no key.

Enable HTTPS on the tailnet (one-time, in the Tailscale admin console under
**DNS → HTTPS Certificates**), then note your machine's full name:

```bash
tailscale status --json | grep -i dnsname
# e.g. research.tail1a2b3c.ts.net
```

## 3. Install the app

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

git clone https://github.com/snunezcr/paper-grabber
cd paper-grabber
uv venv && uv pip install -e .
```

## 4. Bring your data across

From the laptop, copy the ledger, the OpenAlex cache, and your config. This is
the whole migration -- the data is a few hundred kilobytes.

`research-stream` below is the VM's Tailscale name (its hostname); `tailscale
status` on the laptop shows it. These commands assume the laptop is on the same
tailnet.

```bash
# on the laptop
rsync -av ~/.local/share/paper-grabber/ research-stream:.local/share/paper-grabber/
rsync -av ~/.cache/paper-grabber/       research-stream:.cache/paper-grabber/
rsync -av ~/.config/paper-grabber/      research-stream:.config/paper-grabber/
scp credentials.json research-stream:paper-grabber/credentials.json
```

`credentials.json`, the OpenAlex key in `~/.config/paper-grabber/env`, and the
token all move unchanged. Confirm the env file is owner-only on the server:

```bash
chmod 600 ~/.config/paper-grabber/env
```

## 4b. Optional settings

`~/.config/paper-grabber/env` on the server holds configuration that survives
updates (the systemd unit reads it, and a `git pull` cannot overwrite it):

```bash
PG_REFRESH_DAYS=90     # how far back Check now looks (default 7)
PG_MAX_PDF_MB=250      # download size cap (default 100)
OPENALEX_API_KEY=...   # raises the metered daily allowance
OPENALEX_MAILTO=...
```

Restart after changing it:

```bash
systemctl --user restart research-stream.service
```

A wide window costs nothing on later runs -- already-processed messages are
skipped by message id -- but the *first* check after widening will pull
everything in that window at once, and each new paper costs one OpenAlex
lookup.

## 5. Point Google at the server's HTTPS name

The OAuth redirect URI must match the origin the browser uses. In the Google
Cloud console, add to your OAuth client's **Authorised redirect URIs**:

```
https://research.tail1a2b3c.ts.net/auth/google/callback
```

(Substitute your own `*.ts.net` name.) Leave the existing `localhost` entry --
you can still sign in from the server itself if you ever need to.

## 6. Run it as a service

```bash
cp deploy/research-stream.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now research-stream.service

# Let the service keep running after you log out.
sudo loginctl enable-linger $(whoami)
```

Expose it on the tailnet over HTTPS -- Tailscale terminates TLS and proxies to
the loopback port:

```bash
tailscale serve --bg 8823
```

Check both:

```bash
systemctl --user status research-stream.service
tailscale serve status
```

## 7. Use it

From any device on your tailnet, including the Tab S10:

```
https://research.tail1a2b3c.ts.net
```

Sign in with Google once from there -- the HTTPS origin is now a registered
redirect, so it works directly from the tablet, not only the laptop. **Check
now** pulls new alerts; everything else is the UI you already know.

## Updating

```bash
gcloud compute ssh research-stream --zone=us-east1-b
cd paper-grabber && git pull && uv pip install -e .
systemctl --user restart research-stream.service
```

## What this costs, and what it does not buy

- **~$13/month** for an e2-small, or **$0** on the e2-micro free tier with the
  RAM caveat above. Tailscale is free at this scale (up to 3 users, 100
  devices).
- **Egress.** PDFs upload from the VM to Drive, which counts as network egress;
  the free tier includes 1 GB/month from North America, and beyond that it is
  about $0.12/GB. A heavy month of large PDFs might nudge past 1 GB. Downloads
  into the VM are ingress and free.
- It is still **single-user**. The tailnet is the security boundary; the app
  gains no accounts or authentication. Do not run `tailscale funnel` (which
  would publish it to the open internet) or bind anything but `127.0.0.1`.
- Staging is the server's local disk, which is fine now that one machine does
  everything. If the server is ever destroyed, re-run step 4 from the laptop
  copy -- the ledger is the only irreplaceable state, and a few hundred KB.

## Backups

The ledger is small and worth keeping. A weekly copy to your laptop:

```bash
rsync -av research-stream:.local/share/paper-grabber/state.db ~/backups/research-stream/
```

## Alternative: Hetzner Cloud

The app does not care where it runs. Hetzner is roughly half the price of an
e2-small and has two US datacenters — **Ashburn, Virginia** and **Hillsboro,
Oregon**. Only step 1 differs.

The Intel `CX` line is Europe-only, so in the US use the AMD `CPX` line: a
**CPX21** (3 vCPU, 4 GB) is the match, around $6–7/month plus an IPv4 fee.

In the Hetzner console, create the CPX21 with Ubuntu 24.04 in Ashburn, then, as
root:

```bash
adduser --gecos "" research && usermod -aG sudo research
install -d -o research -g research /home/research/.ssh
cp ~/.ssh/authorized_keys /home/research/.ssh/
chown research:research /home/research/.ssh/authorized_keys

ufw allow OpenSSH && ufw --force enable
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

Reconnect as `research@<public-ip>`, then follow **step 2 onward** exactly as
above — Tailscale, install, migration, and the service are identical.
