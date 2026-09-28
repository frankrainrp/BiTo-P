# Security boundary

Run this showcase on loopback with synthetic data. The Docker port is bound to `127.0.0.1` by default.

## Identity

The application currently selects a member by username. It does not verify a password, passkey or external identity. A person who knows another member's username can sign in as that member. Do not expose this login to untrusted networks. Session ownership checks and same-origin checks do not solve this identity limitation.

## Data and integrations

- The example creates and removes a temporary database. Real household data, screenshots, tokens and model weights are excluded.
- Runtime secrets belong in server-side environment variables; `.env` and data directories are ignored.
- AI tool output is untrusted. Model-proposed writes go through member-bound confirmation and domain validation.
- Website monitoring restricts destinations and uses rate/cooldown limits. Do not weaken those checks to access arbitrary internal URLs.
- The Windows capture agent is optional source; running the demo does not install or start it.

## Reporting

For non-sensitive defects, open an issue with a synthetic reproduction. Do not include credentials, real conversations, household databases or screenshots containing personal information in a public issue.
