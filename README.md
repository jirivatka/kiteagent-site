# kiteagent.app

Static site for Kite Agent: landing page, support, privacy policy.

Three HTML files and one stylesheet. No build step, no dependencies, no external
assets — the only outbound link is to tailscale.com. That matters more than it
sounds: App Review fetches these pages from unpredictable networks, and a page
that depends on a CDN is a page that can fail review.

## Why these pages exist

⚠️ **App Store submission requires a Support URL and a Privacy Policy URL.** Both
are mandatory. `support.html` and `privacy.html` are those pages.

The support page is built from problems that actually occurred in testing rather
than imagined ones — the red dot at home versus away, firewalls silently blocking
incoming connections, the Notification Center toggle, why a different model
answered. It leads with "send us your connection log" because that is what
actually diagnoses connection problems.

The privacy policy is short because the truth is short: no analytics, no tracking,
no account, no backend. Before editing it, check the claims still hold — the app
currently talks only to the user's own machine and, in direct mode, to a provider
the user chose. If that ever changes, this page must change in the same release.

## Deploy

GitHub Pages from the default branch. `CNAME` pins the custom domain.
