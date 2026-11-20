# TURN relay for live interview video (13 Nov 2026)

Who: the server admin / DevOps. Needed once per production environment.

## Why

On the Interview Integrity tab a recruiter can now **call** the candidate (from the moment the link
opens) and **watch the interview in real time** (camera + entire screen). That video goes browser to
browser with WebRTC. On most home networks a public STUN server is enough, but office networks,
mobile hotspots and strict home routers block the direct path — the call or the live view then
stays on "Connecting…". A TURN server relays the media in that case.

Without TURN everything else still works: the ~15-second-behind recording view, pause, notes,
queued questions and Terminate. The live panel shows an amber note while no TURN is configured.

## Install coturn (Ubuntu, same VPC as the app or a small separate VM)

```bash
sudo apt-get install -y coturn
sudo sed -i 's/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/' /etc/default/coturn
```

`/etc/turnserver.conf`:

```
listening-port=3478
tls-listening-port=5349
fingerprint
lt-cred-mech
user=karnex:<a long random password>
realm=turn.karnexgroup.com
total-quota=100
stale-nonce=600
cert=/etc/letsencrypt/live/turn.karnexgroup.com/fullchain.pem
pkey=/etc/letsencrypt/live/turn.karnexgroup.com/privkey.pem
no-multicast-peers
no-cli
# relay ports
min-port=49160
max-port=49200
```

Open in the firewall / security group: UDP+TCP 3478, TCP 5349, UDP 49160–49200.

```bash
sudo systemctl enable --now coturn
```

## Tell the app

In the backend environment (`/etc/karnex/karnex.env`):

```
INTERVIEW_TURN_URLS=turn:turn.karnexgroup.com:3478,turns:turn.karnexgroup.com:5349
INTERVIEW_TURN_USERNAME=karnex
INTERVIEW_TURN_CREDENTIAL=<the same password>
```

Restart the backend (`sudo karnex-rolling-restart`). The live panel's amber "No TURN relay" note
disappears.

## Check it

Open https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/, add
`turn:turn.karnexgroup.com:3478` with the user and password, press **Gather candidates** — a
`relay` candidate must appear.

## Limits

* At most **2 recruiters watch** one interview at once (`interview_live.MAX_WATCHERS`) — each
  watcher is a stream from the candidate's laptop.
* Bandwidth per watcher ≈ 0.6–1.5 Mbit/s while relayed.
