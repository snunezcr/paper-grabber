# Running Research Stream on a Hetzner Cloud server

An always-on deployment for one person, reached over Tailscale. Roughly
$6/month and about half an hour to set up.

**US location.** Hetzner Cloud has two US datacenters — **Ashburn, Virginia**
(`us-east`) and **Hillsboro, Oregon** (`us-west`) — so there is no need to
change providers for a US-based service. Pick whichever is closer to you;
Ashburn is the better choice for the Midwest and East Coast.

**Instance type.** The Intel `CX` line (including the CX22) is Europe-only. The
US datacenters run the AMD `CPX` line instead. The right match here is
**CPX21** — 3 vCPU, 4 GB RAM — because a PDF download is buffered in memory up
to a 200 MB cap, and 4 GB leaves comfortable headroom. CPX11 (2 vCPU, 2 GB)
works for ordinary papers but is tight if a large download coincides with
anything else. Prices shift, so confirm in the console; CPX21 is around
$6–7/month plus Hetzner's small IPv4 fee.

Everything below is identical whichever location and CPX size you choose.

The shape of it:

```
tablet / laptop ──(Tailscale, HTTPS)──▶ CPX21 (Ashburn) ──▶ 127.0.0.1:8823
```

The app binds **loopback only** and is never exposed to the public internet.
Tailscale is what makes it reachable, and only from your own devices. This
matters: the web API has no authentication, so a public bind would hand Drive
browsing and filing to anyone who found the port.

## 1. Create the server

In the Hetzner Cloud console: **CPX21** (3 vCPU, 4 GB), Ubuntu 24.04, location
**Ashburn** or **Hillsboro**. Add your SSH key. Note the public IP.

```bash
ssh root@<public-ip>
```

Create a non-root user and stop using root:

```bash
adduser --gecos "" research
usermod -aG sudo research
install -d -o research -g research /home/research/.ssh
cp ~/.ssh/authorized_keys /home/research/.ssh/
chown research:research /home/research/.ssh/authorized_keys
```

Lock the box down. The firewall leaves **nothing** open but SSH — every other
port, including 8823, is reached through Tailscale, not the public interface.

```bash
ufw allow OpenSSH
ufw --force enable
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

Reconnect as the new user: `ssh research@<public-ip>`.

## 2. Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

Follow the printed URL to authorise the machine into your tailnet. From now on
you can reach it as `research` (or its `*.ts.net` name) from any of your
devices, and `tailscale ssh research@research` needs no key.

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

```bash
# on the laptop
rsync -av ~/.local/share/paper-grabber/ research:.local/share/paper-grabber/
rsync -av ~/.cache/paper-grabber/       research:.cache/paper-grabber/
rsync -av ~/.config/paper-grabber/      research:.config/paper-grabber/
scp credentials.json research:paper-grabber/credentials.json
```

`credentials.json`, the OpenAlex key in `~/.config/paper-grabber/env`, and the
token all move unchanged. Confirm the env file is owner-only on the server:

```bash
chmod 600 ~/.config/paper-grabber/env
```

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
sudo loginctl enable-linger research
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
ssh research@research
cd paper-grabber && git pull && uv pip install -e .
systemctl --user restart research-stream.service
```

## What this costs, and what it does not buy

- **~$6–7/month** for the CPX21, plus Hetzner's small IPv4 fee. Tailscale is
  free at this scale (up to 3 users, 100 devices).
- It is still **single-user**. The tailnet is the security boundary; the app
  gains no accounts or authentication. Do not run `tailscale funnel` (which
  would publish it to the open internet) or bind anything but `127.0.0.1`.
- Staging is the server's local disk, which is fine now that one machine does
  everything. If the server is ever destroyed, re-run step 4 from the laptop
  copy -- the ledger is the only irreplaceable state, and a few hundred KB.

## Backups

The ledger is small and worth keeping. A weekly copy to your laptop:

```bash
rsync -av research:.local/share/paper-grabber/state.db ~/backups/research-stream/
```
