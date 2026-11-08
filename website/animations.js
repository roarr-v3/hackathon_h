(() => {
  if (!window.gsap || !window.ScrollTrigger || !window.MotionPathPlugin) return;

  gsap.registerPlugin(ScrollTrigger, MotionPathPlugin);
  const media = gsap.matchMedia();

  media.add('(prefers-reduced-motion: no-preference)', () => {
    const intro = gsap.timeline({ defaults: { ease: 'power3.out' } });
    intro
      .from('.nav', { y: -18, opacity: 0, duration: 0.6 })
      .from('.hero .eyebrow', { y: 16, opacity: 0, duration: 0.5 }, '-=0.3')
      .from('.headline-word', { y: 60, rotation: 5, opacity: 0, stagger: 0.11, duration: 0.8 }, '-=0.25')
      .from('.hero-copy, .hero-actions', { y: 22, opacity: 0, stagger: 0.1, duration: 0.6 }, '-=0.5')
      .from('.hero-raven', { x: 120, y: -130, rotation: 22, scale: 0.6, opacity: 0, duration: 1.2, ease: 'back.out(1.2)' }, 0.35)
      .from('.bubble', { scale: 0.75, opacity: 0, stagger: 0.2, duration: 0.5 }, '-=0.4');

    gsap.to('.hero-raven', { y: -12, rotation: -3, duration: 2.5, repeat: -1, yoyo: true, ease: 'sine.inOut', delay: 2 });
    gsap.to('.star', { rotation: '+=35', y: -8, stagger: 0.3, duration: 3, repeat: -1, yoyo: true, ease: 'sine.inOut' });
    gsap.to('.reading-progress', { scaleX: 1, ease: 'none', scrollTrigger: { trigger: 'body', start: 'top top', end: 'bottom bottom', scrub: true } });

    for (const selector of ['.section-heading', '.steps article', '.platform-grid > div', '.demo-card > div', '.surface-grid article', '.closing > *']) {
      gsap.utils.toArray(selector).forEach((element, index) => {
        gsap.from(element, { y: 35, opacity: 0, duration: 0.8, delay: selector === '.steps article' ? index * 0.1 : 0, ease: 'power2.out', scrollTrigger: { trigger: element, start: 'top 91%', toggleActions: 'play none none reverse' } });
      });
    }

    gsap.from('.demo-terminal p, .terminal-verdict', { x: -14, opacity: 0, stagger: 0.28, duration: 0.5, scrollTrigger: { trigger: '.demo-terminal', start: 'top 80%', toggleActions: 'play none none reverse' } });
    gsap.to('.perched-raven', { y: -25, rotation: -8, ease: 'none', scrollTrigger: { trigger: '.platform-section', start: 'top bottom', end: 'bottom top', scrub: 1 } });

    const sponsors = document.querySelector('.partners .wrap > div');
    const clones = [...sponsors.children].map((item) => {
      const clone = item.cloneNode(true);
      clone.setAttribute('aria-hidden', 'true');
      sponsors.append(clone);
      return clone;
    });
    gsap.to(sponsors, { xPercent: -50, duration: 24, repeat: -1, ease: 'none' });

    return () => clones.forEach((clone) => clone.remove());
  });

  media.add('(min-width: 900px) and (prefers-reduced-motion: no-preference)', () => {
    const bird = document.createElement('img');
    bird.src = 'assets/raven-small.webp';
    bird.alt = '';
    bird.className = 'flight-bird';
    bird.setAttribute('aria-hidden', 'true');
    const steps = document.querySelector('.steps');
    steps.append(bird);
    const flight = gsap.to(bird, {
      motionPath: { path: [{ x: 0, y: 0 }, { x: steps.clientWidth * 0.3, y: -45 }, { x: steps.clientWidth * 0.65, y: -15 }, { x: steps.clientWidth - 70, y: 0 }], curviness: 1.2 },
      rotation: 8, ease: 'none',
      scrollTrigger: { trigger: '#how-it-works', start: 'top 65%', end: 'bottom 30%', scrub: 0.8, invalidateOnRefresh: true },
    });
    const recalculate = () => flight.invalidate();
    window.addEventListener('resize', recalculate);
    return () => { window.removeEventListener('resize', recalculate); bird.remove(); };
  });

  media.add('(min-width: 900px) and (hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)', () => {
    const art = document.querySelector('.hero-art');
    const moveArtX = gsap.quickTo(art, 'x', { duration: 0.8, ease: 'power2.out' });
    const moveArtY = gsap.quickTo(art, 'y', { duration: 0.8, ease: 'power2.out' });
    const move = (event) => { moveArtX((event.clientX / innerWidth - 0.5) * 22); moveArtY((event.clientY / innerHeight - 0.5) * 16); };
    const reset = () => { moveArtX(0); moveArtY(0); };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerleave', reset);
    const cleanup = [];
    document.querySelectorAll('.button').forEach((button) => {
      const x = gsap.quickTo(button, 'x', { duration: 0.4 });
      const y = gsap.quickTo(button, 'y', { duration: 0.4 });
      const enter = (event) => { const box = button.getBoundingClientRect(); x((event.clientX - box.left - box.width / 2) * 0.15); y((event.clientY - box.top - box.height / 2) * 0.2); };
      const leave = () => { x(0); y(0); };
      button.addEventListener('pointermove', enter);
      button.addEventListener('pointerleave', leave);
      cleanup.push(() => { button.removeEventListener('pointermove', enter); button.removeEventListener('pointerleave', leave); });
    });
    return () => { document.removeEventListener('pointermove', move); document.removeEventListener('pointerleave', reset); cleanup.forEach((fn) => fn()); };
  });

  document.fonts.ready.then(() => ScrollTrigger.refresh());
})();
