// Opening splash: Mutz claws through a hive of crawled pages, finds the honey pot and grabs it.
// Shows once per browser (localStorage "mutz-intro"); /?intro=1 replays it. A click or any key skips it.
(() => {
  const html = document.documentElement, box = document.getElementById('intro');
  if (!box || !html.classList.contains('intro')) return;
  try { localStorage.setItem('mutz-intro', '1'); } catch (e) {}

  const R = 26, S = 24, CX = 200, CY = 190, SQ3 = Math.sqrt(3), D = 380;
  const $ = id => document.getElementById('i-' + id);
  const anims = [];
  // Three claw swipes; the last one runs through the centre where the pot hides.
  const SW = [
    {a:[60,70],  b:[190,330], t:1550},
    {a:[345,80], b:[245,330], t:2250},
    {a:[70,116], b:[330,264], t:2950},
  ];
  SW.forEach(s => {
    const dx = s.b[0]-s.a[0], dy = s.b[1]-s.a[1], L = Math.hypot(dx,dy);
    Object.assign(s, {L, u:[dx/L,dy/L], n:[-dy/L,dx/L], ang:Math.atan2(dy,dx)*180/Math.PI});
  });

  function rngf(seed){return () => {seed = seed+0x6D2B79F5|0; let t = Math.imul(seed^seed>>>15, 1|seed); t = t+Math.imul(t^t>>>7, 61|t)^t; return ((t^t>>>14)>>>0)/4294967296}}
  const f2 = v => v.toFixed(2);
  const pt = p => f2(p[0])+','+f2(p[1]);
  const reg = a => (anims.push(a), a);
  // One animation per element and property, keyframes at absolute ms.
  function track(el, frames){
    const end = frames[frames.length-1][0];
    return reg(el.animate(frames.map(([t,p,e]) => ({...p, offset:t/end, easing:e||'linear'})), {duration:end, fill:'both'}));
  }
  function hexPts(x,y,s){
    const out = [];
    for (let k=0;k<6;k++){ const a = (60*k-90)*Math.PI/180; out.push([x+s*Math.cos(a), y+s*Math.sin(a)]); }
    return out;
  }
  // A cell is one crawled page: hairline hexagon, a black title line, grey text lines.
  function page(c, rng){
    let s = `<polygon points="${hexPts(c.x,c.y,S).map(pt).join(' ')}" fill="#fff" stroke="#111" stroke-width="1"/>`;
    const x0 = c.x-14;
    s += `<line x1="${f2(x0)}" y1="${f2(c.y-9)}" x2="${f2(x0+12+rng()*9)}" y2="${f2(c.y-9)}" stroke="#111" stroke-width="2.6"/>`;
    for (const dy of [-2.5,3,8.5]) s += `<line x1="${f2(x0)}" y1="${f2(c.y+dy)}" x2="${f2(x0+18+rng()*11)}" y2="${f2(c.y+dy)}" stroke="#A8A8A8" stroke-width="1.5"/>`;
    return s;
  }
  function segHit(c, s){
    const px = c.x-s.a[0], py = c.y-s.a[1];
    const t = Math.max(0, Math.min(1, (px*s.u[0]+py*s.u[1])/s.L));
    return {t, d:Math.hypot(c.x-(s.a[0]+s.u[0]*t*s.L), c.y-(s.a[1]+s.u[1]*t*s.L))};
  }

  function build(){
    const rng = rngf(11), defs = [], cells = [];
    for (let q=-3;q<=3;q++) for (let r=-3;r<=3;r++){
      const ring = Math.max(Math.abs(q),Math.abs(r),Math.abs(q+r));
      if (ring<=3) cells.push({x:CX+R*SQ3*(q+r/2), y:CY+R*1.5*r, ring});
    }
    let out = '';
    cells.forEach((c,i) => {
      c.body = page(c, rng);
      for (let k=0;k<3;k++){
        const h = segHit(c, SW[k]);
        if (h.d < 21 || (k===2 && c.ring<=1)){ c.k = k; c.tear = SW[k].t + h.t*D + 40; break; }
      }
      let inner = `<g class="whole" style="transform-origin:${f2(c.x)}px ${f2(c.y)}px">${c.body}</g>`;
      if (c.k !== undefined){
        const {u,n} = SW[c.k], cut = [];
        for (let s=-34;s<=34;s+=4){ const j = (s===-34||s===34) ? 0 : (rng()-.5)*4.4; cut.push([c.x+u[0]*s+n[0]*j, c.y+u[1]*s+n[1]*j]); }
        const side = sg => cut.concat([[c.x+u[0]*34+n[0]*34*sg, c.y+u[1]*34+n[1]*34*sg],[c.x-u[0]*34+n[0]*34*sg, c.y-u[1]*34+n[1]*34*sg]]).map(pt).join(' ');
        defs.push(`<clipPath id="i-hA${i}"><polygon points="${side(1)}"/></clipPath><clipPath id="i-hB${i}"><polygon points="${side(-1)}"/></clipPath><clipPath id="i-hx${i}"><polygon points="${hexPts(c.x,c.y,S).map(pt).join(' ')}"/></clipPath>`);
        const edge = `<polyline points="${cut.map(pt).join(' ')}" clip-path="url(#i-hx${i})" fill="none" stroke="#9A9A9A" stroke-width="1.3"/>`;
        const half = (id,sg) => `<g class="h" data-sg="${sg}" style="transform-origin:${f2(c.x+n[0]*9*sg)}px ${f2(c.y+n[1]*9*sg)}px"><g clip-path="url(#${id})">${c.body}${edge}</g></g>`;
        inner += `<g class="halves" opacity="0">${half('i-hA'+i,1)}${half('i-hB'+i,-1)}</g>`;
      }
      out += `<g class="cell" style="transform-origin:${f2(c.x)}px ${f2(c.y)}px">${inner}</g>`;
    });
    $('defs').innerHTML = defs.join('');
    $('hive').innerHTML = out;
    $('claws').innerHTML = SW.map(s => [-8.5,0,8.5].map(o => {
      const a = [s.a[0]+s.n[0]*o, s.a[1]+s.n[1]*o], b = [s.b[0]+s.n[0]*o, s.b[1]+s.n[1]*o];
      return `<line x1="${f2(a[0])}" y1="${f2(a[1])}" x2="${f2(b[0])}" y2="${f2(b[1])}" stroke="#111" stroke-width="2.6" stroke-linecap="round" stroke-dasharray="${f2(s.L)} ${f2(s.L+10)}" stroke-dashoffset="${f2(s.L)}"/>`;
    }).join('')).join('');
    $('paws').innerHTML = SW.map(() => `<g class="paw" opacity="0">
      <path d="M9 -8.5H21M11 0H24M9 8.5H21" stroke="#111" stroke-width="3.4" stroke-linecap="round"/>
      <ellipse rx="14" ry="12.5" fill="#111"/><ellipse cx="-3" rx="5.5" ry="4.5" fill="#fff"/>
      <circle cx="5" cy="-6.5" r="1.9" fill="#fff"/><circle cx="7.5" r="1.9" fill="#fff"/><circle cx="5" cy="6.5" r="1.9" fill="#fff"/></g>`).join('');
    return {cells, rng};
  }

  function run(){
    const {cells, rng} = build();
    const ease = 'cubic-bezier(.5,0,.85,.6)'; // paper falling: slow start, gravity at the end

    // The hive of pages builds outward; cells on a swipe tear in two and fall, the rest crumble once the pot is found.
    box.querySelectorAll('#i-hive .cell').forEach((el,i) => {
      const c = cells[i];
      reg(el.animate([{transform:'scale(0)',opacity:0},{transform:'scale(1.08)',opacity:1,offset:.6},{transform:'scale(1)',opacity:1}],
        {duration:520, delay:c.ring*170+rng()*90, easing:'ease-out', fill:'both'}));
      const whole = el.querySelector('.whole');
      if (c.k !== undefined){
        reg(whole.animate([{opacity:1},{opacity:0}], {delay:c.tear, duration:1, fill:'both'}));
        reg(el.querySelector('.halves').animate([{opacity:0},{opacity:1}], {delay:c.tear, duration:1, fill:'both'}));
        const n = SW[c.k].n;
        el.querySelectorAll('.h').forEach(h => {
          const sg = +h.dataset.sg, rot = sg*(30+rng()*35), dx = n[0]*sg, dy = n[1]*sg;
          reg(h.animate([
            {transform:'translate(0px,0px) rotate(0deg)', opacity:1},
            {transform:`translate(${f2(dx*8)}px,${f2(dy*8-3)}px) rotate(${f2(rot*.15)}deg)`, opacity:1, offset:.18},
            {transform:`translate(${f2(dx*36+(rng()-.5)*20)}px,320px) rotate(${f2(rot)}deg)`, opacity:0}
          ], {duration:1200+rng()*250, delay:c.tear, easing:ease, fill:'both'}));
        });
      } else {
        const ox = (c.x-CX)*.25+(rng()-.5)*20, rot = (rng()-.5)*90;
        reg(whole.animate([
          {transform:'translate(0px,0px) rotate(0deg)', opacity:1},
          {transform:`translate(${f2(ox*.2)}px,-6px) rotate(${f2(rot*.1)}deg)`, opacity:1, offset:.15},
          {transform:`translate(${f2(ox)}px,320px) rotate(${f2(rot)}deg)`, opacity:0}
        ], {duration:1050, delay:3350+Math.hypot(c.x-CX,c.y-CY)*1.8+rng()*160, easing:ease, fill:'both'}));
      }
    });

    const still = {transform:'translate(0px,0px)'}, jolt = [[0,still]];
    SW.forEach(s => { const hit = s.t+D; jolt.push([hit-1,still],[hit+60,{transform:'translate(1.5px,2px)'}],[hit+180,still]); });
    jolt.push([5800,still]); track($('hive'), jolt);

    // Claw swipes: the paw leads, three scratch marks draw behind it.
    const paws = box.querySelectorAll('#i-paws .paw'), lines = box.querySelectorAll('#i-claws line');
    SW.forEach((s,k) => {
      const at = d => `translate(${f2(s.a[0]+s.u[0]*d)}px,${f2(s.a[1]+s.u[1]*d)}px) rotate(${f2(s.ang)}deg)`;
      track(paws[k], [[0,{opacity:0,transform:at(-60)}],[s.t-140,{opacity:0,transform:at(-60)},'ease-out'],
        [s.t,{opacity:1,transform:at(-22)},'cubic-bezier(.5,0,.7,1)'],[s.t+D,{opacity:1,transform:at(s.L-22)},'ease-out'],
        [s.t+D+200,{opacity:0,transform:at(s.L+20)}]]);
      for (let j=0;j<3;j++){
        const L = f2(s.L)+'px';
        track(lines[k*3+j], [[0,{strokeDashoffset:L,opacity:1}],[s.t,{strokeDashoffset:L,opacity:1},'ease-in'],
          [s.t+D,{strokeDashoffset:'0px',opacity:1}],[s.t+D+350,{strokeDashoffset:'0px',opacity:1},'ease-out'],
          [s.t+D+1000,{strokeDashoffset:'0px',opacity:0}]]);
      }
    });

    // Mutz rises in, leans into each swipe, jumps when the pot is full, then grabs it.
    const at0 = 'translate(0px,0px)';
    track($('mzMove'), [[0,{transform:'translate(0px,170px)'}],[650,{transform:'translate(0px,170px)'},'cubic-bezier(.25,1.35,.45,1)'],
      [1300,{transform:at0}],[4500,{transform:at0},'cubic-bezier(.6,0,.25,1)'],[5150,{transform:'translate(-135px,-147px)'}]]);
    const hop = (y,r) => ({transform:`translateY(${y}px) rotate(${r}deg)`}), rest = hop(0,0);
    track($('mzHop'), [[0,rest],[1300,rest,'ease-out'],[1500,hop(-4,-9),'ease-in-out'],[1950,rest],
      [2000,rest,'ease-out'],[2200,hop(-4,8),'ease-in-out'],[2650,rest],
      [2700,rest,'ease-out'],[2900,hop(-4,-5),'ease-in-out'],[3350,rest],
      [4150,rest,'ease-out'],[4300,hop(-10,0),'ease-in'],[4430,rest],
      [5150,rest,'ease-out'],[5330,hop(-3,7),'ease-in-out'],[5480,hop(0,4)]]);
    track($('grab'), [[0,{opacity:0,transform:'scale(.3)'}],[5100,{opacity:0,transform:'scale(.3)'},'cubic-bezier(.3,1.5,.5,1)'],[5320,{opacity:1,transform:'scale(1)'}]]);
    const eye = (o,s) => ({opacity:o, transform:`scaleY(${s})`});
    track($('eyes'), [[0,eye(1,1)],[1300,eye(1,1)],[1360,eye(1,.1)],[1440,eye(1,1)],[3200,eye(1,1)],[3260,eye(1,.1)],[3340,eye(1,1)],[4150,eye(1,1)],[4200,eye(0,1)]]);
    track($('happy'), [[0,{opacity:0}],[4150,{opacity:0}],[4200,{opacity:1}]]);
    ['earL','earR'].forEach((id,i) => {
      const y = v => ({transform:`translateY(${v}px)`}), o = i*80;
      track($(id), [[0,y(0)],[1300+o,y(0)],[1360+o,y(-1.5)],[1440+o,y(0)],[4150+o,y(0)],[4210+o,y(-2)],[4300+o,y(0)],[4330+o,y(-1.2)],[4400+o,y(0)]]);
    });

    // The pot appears where the last swipe opened the hive, fills thin then strong, the lid pops.
    const pot = (o,x,y,s,r=0) => ({opacity:o, transform:`translate(${x}px,${y}px) rotate(${r}deg) scale(${s})`});
    track($('pot'), [[0,pot(0,0,0,.9)],[3080,pot(0,0,0,.9),'ease-out'],[3350,pot(1,0,0,1)],[4500,pot(1,0,0,1),'cubic-bezier(.6,0,.25,1)'],
      [5150,pot(1,38,-29.75,1),'ease-out'],[5330,pot(1,36,-35,1,-8),'ease-in-out'],[5480,pot(1,37,-32,1,-5)]]);
    const lvl = v => ({transform:`translateY(${v}px)`});
    track($('honey'), [[0,lvl(126)],[3350,lvl(126),'ease-out'],[3650,lvl(100)],[3780,lvl(100),'cubic-bezier(.3,0,.2,1)'],
      [4100,lvl(33),'ease-in-out'],[4350,lvl(37.5),'ease-in-out'],[4550,lvl(36)]]);
    reg($('wave').animate([{transform:'translateX(0px)'},{transform:'translateX(-50px)'}], {duration:1000, delay:3300, iterations:2.5, fill:'both'}));
    const lid = (y,r) => ({transform:`translate(0px,${y}px) rotate(${r}deg)`});
    track($('lid'), [[0,lid(0,0)],[4100,lid(0,0),'ease-out'],[4230,lid(-14,-16),'ease-in'],[4380,lid(0,0),'ease-out'],[4440,lid(-3,3),'ease-in'],[4500,lid(0,0)]]);

    const up = (o,y) => ({opacity:o, transform:`translateY(${y}px)`});
    track($('word'), [[0,up(0,10)],[5000,up(0,10),'cubic-bezier(.2,.7,.2,1)'],[5450,up(1,0)]]);
    return track($('tag'), [[0,up(0,8)],[5150,up(0,8),'cubic-bezier(.2,.7,.2,1)'],[5600,up(1,0)]]);
  }

  let closed = false;
  function close(){
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', skip);
    box.animate([{opacity:1},{opacity:0}], {duration:400, easing:'ease-out'}).onfinish = () => { html.classList.remove('intro'); box.remove(); };
  }
  function skip(){ anims.forEach(a => a.finish()); setTimeout(close, 250); }

  // Hold the logo frame for a moment, then fade into the app.
  run().finished.then(() => setTimeout(close, 1200));
  box.addEventListener('click', skip);
  document.addEventListener('keydown', skip);
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) anims.forEach(a => a.finish());
})();
