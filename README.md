# Raven website

A monochrome static landing page starring the Raven mascot. No build step required. Content and navigation work without JavaScript.

From this directory, run `python3 -m http.server 8080`, then open http://localhost:8080.

The page uses Google Fonts with local system fallbacks. Mascot assets are derived from the Raven application icon in `t3code/assets/prod/app-icon.icon/Assets/raven.png`.

Animation uses GSAP 3.15.1 with ScrollTrigger and MotionPathPlugin, vendored from the official npm package in `vendor/`. See https://gsap.com/standard-license/ for its license. Reduced-motion preferences disable animation, including pointer effects and the sponsor marquee.
