/**
 * Re-measure the six `fit` insets against the chrome they name.
 *
 *     npm run measure
 *
 * `App.tsx` holds six numbers that keep `fit` from finishing underneath the
 * topbar, the legend or an open panel. They are the *extent* of a floating
 * element, so a rule in `index.css` can move one without touching the number
 * that stands for it — which is how four of the six came to be 20 to 28px short
 * of the chrome they name and stayed that way through a redesign. Nothing in
 * the test suite sees it: jsdom has no layout, so the only honest check is a
 * browser.
 *
 * This starts vite on the mock canvas, measures `.topbar__plate` and `.legend`
 * at four widths and then the two panels with one open, and prints the
 * clearance `fit` actually leaves. A negative clearance is a card drawn behind
 * the chrome and exits non-zero; the rest is for a person to look at, because
 * "enough room" is a judgement and 30/18 is the one this design made.
 *
 * Deliberately not in CI. There is no browser on the runner, and puppeteer-core
 * is not a dependency of this package — it is here if the machine happens to
 * have it, and says so plainly if it does not.
 */

import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { globSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);

/** The widths worth looking at: two above the narrow query, two below it. */
const WIDTHS = [
  [1440, 900],
  [1024, 800],
  [719, 844],
  [390, 844],
];

/** Read the numbers out of the source, so this cannot drift from what ships. */
function constants() {
  const app = readFileSync(join(root, 'src/App.tsx'), 'utf8');
  const canvas = readFileSync(join(root, 'src/features/topology/ui/TopologyCanvas.tsx'), 'utf8');
  const number = (text, name, pattern) => {
    const found = text.match(pattern);
    if (!found) throw new Error(`could not find ${name} — has it been renamed?`);
    return Number(found[1]);
  };
  return {
    top: number(app, 'TOPBAR_INSET', /const TOPBAR_INSET = (\d+)/),
    topNarrow: number(app, 'TOPBAR_INSET_NARROW', /const TOPBAR_INSET_NARROW = (\d+)/),
    bottom: number(app, 'LEGEND_INSET', /const LEGEND_INSET = (\d+)/),
    bottomNarrow: number(app, 'LEGEND_INSET_NARROW', /const LEGEND_INSET_NARROW = (\d+)/),
    left: number(app, 'HOSTS_INSET', /const HOSTS_INSET = (\d+)/),
    right: number(app, 'INSPECTOR_INSET', /const INSPECTOR_INSET = (\d+)/),
    pad: number(canvas, "fit's padding", /const padding = (\d+)/),
    narrowQuery: app.match(/const NARROW_QUERY = '([^']+)'/)?.[1] ?? '(max-width: 720px)',
  };
}

/** The headless shell, not chrome: the full binary never completes a loopback
 *  fetch on this machine — every navigation hangs with no failure. */
function browserPath() {
  const found = globSync(
    `${process.env.HOME}/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell`,
  ).sort();
  return found.at(-1);
}

function startVite(port) {
  const child = spawn('npx', ['vite', '--port', String(port), '--strictPort'], {
    cwd: root,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const ready = new Promise((resolve, reject) => {
    const deadline = setTimeout(() => reject(new Error('vite did not start within 30s')), 30_000);
    child.stdout.on('data', (chunk) => {
      if (String(chunk).includes('Local:')) {
        clearTimeout(deadline);
        resolve();
      }
    });
    child.on('exit', (code) => reject(new Error(`vite exited with ${code}`)));
  });
  return { child, ready };
}

/** A real mouse press at the centre of the first match, by selector and — when
 *  the selector is not enough on its own — the text the element starts with. */
async function click(page, selector, startsWith = '') {
  const box = await page.evaluate(
    (sel, text) => {
      const match = [...document.querySelectorAll(sel)].find((node) =>
        node.textContent.trim().startsWith(text),
      );
      if (!match) return null;
      const rect = match.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    },
    selector,
    startsWith,
  );
  if (!box) throw new Error(`nothing matched ${selector}${startsWith ? ` starting "${startsWith}"` : ''}`);
  await page.mouse.click(box.x, box.y);
  await new Promise((resolve) => setTimeout(resolve, 500));
}

/** Open a panel, measure the edge `fit` has to stay clear of, and close it. */
async function panel(page, { label, side, selector, inset, open }) {
  await open();
  const edge = await page.evaluate(
    (sel, which) => {
      const rect = document.querySelector(sel)?.getBoundingClientRect();
      if (!rect) return null;
      return which === 'left' ? rect.right : window.innerWidth - rect.left;
    },
    selector,
    side,
  );
  if (edge === null) {
    console.error(`  ${label}: never opened — has the panel or the way in changed?`);
    return -1;
  }
  const clearance = inset - edge;
  console.log(
    `  ${label} (${side})  panel reaches ${edge.toFixed(0)}, fit stops at ${inset} → ${clearance.toFixed(0)}px`,
  );
  await page.keyboard.press('Escape');
  await new Promise((resolve) => setTimeout(resolve, 400));
  return clearance;
}

async function main() {
  const exe = browserPath();
  if (!exe) {
    console.error('No chrome-headless-shell under ~/.cache/ms-playwright — nothing measured.');
    return 2;
  }
  let puppeteer;
  try {
    puppeteer = require('puppeteer-core');
  } catch {
    console.error('puppeteer-core is not installed here — nothing measured.');
    return 2;
  }

  const c = constants();
  const port = 5100 + Math.floor(Math.random() * 300);
  const vite = startVite(port);
  let browser;
  let worst = Infinity;

  try {
    await vite.ready;
    browser = await puppeteer.launch({ executablePath: exe, args: ['--no-sandbox'] });
    const page = await browser.newPage();
    // `?mock=1` paints the canned graph, so this needs no Controller.
    await page.goto(`http://127.0.0.1:${port}/?mock=1`, { waitUntil: 'networkidle0' });

    console.log(`fit padding ${c.pad}px · narrow at ${c.narrowQuery}\n`);
    for (const [width, height] of WIDTHS) {
      await page.setViewport({ width, height });
      // The insets are read on a media-query change; give React the frame.
      await new Promise((resolve) => setTimeout(resolve, 700));

      const seen = await page.evaluate((query) => {
        const box = (selector) => document.querySelector(selector)?.getBoundingClientRect();
        const plate = box('.topbar__plate');
        const legend = box('.legend');
        if (!plate || !legend) return null;
        return {
          plateEnds: plate.bottom,
          legendStarts: window.innerHeight - legend.top,
          narrow: window.matchMedia(query).matches,
        };
      }, c.narrowQuery);

      if (!seen) {
        console.error(`  ${width}×${height}: no topbar or legend on the page — is it the mock canvas?`);
        return 1;
      }

      const contentTop = (seen.narrow ? c.topNarrow : c.top) + c.pad;
      const contentBottom = (seen.narrow ? c.bottomNarrow : c.bottom) + c.pad;
      const above = contentTop - seen.plateEnds;
      const below = contentBottom - seen.legendStarts;
      worst = Math.min(worst, above, below);

      console.log(
        `  ${String(width).padStart(4)}×${height} ${seen.narrow ? 'narrow' : '  wide'}  ` +
          `topbar: plate ends ${seen.plateEnds.toFixed(0)}, fit starts ${contentTop} → ${above.toFixed(0)}px  ` +
          `legend: starts ${seen.legendStarts.toFixed(0)} up, fit ends ${contentBottom} up → ${below.toFixed(0)}px`,
      );
    }
    // The panels, which only exist while one is open. The inspector needs a
    // selection, and the canvas has no DOM to click — the search box is the
    // way in, and it wants real input events rather than a synthetic click.
    await page.setViewport({ width: 1440, height: 900 });
    await new Promise((resolve) => setTimeout(resolve, 400));
    console.log('');

    worst = Math.min(worst, await panel(page, {
      label: 'hosts    ',
      side: 'left',
      selector: '.hosts',
      inset: c.left + c.pad,
      open: () => click(page, '.topbar__btn', 'Hosts'),
    }));

    worst = Math.min(worst, await panel(page, {
      label: 'inspector',
      side: 'right',
      selector: '.inspector',
      inset: c.right + c.pad,
      open: async () => {
        await page.click('.search');
        await page.type('.search', 'grafana');
        await page.waitForSelector('.search__results button', { timeout: 5_000 });
        await click(page, '.search__results button');
      },
    }));
  } finally {
    await browser?.close();
    vite.child.kill();
  }

  if (worst < 0) {
    console.error(`\nfit reaches ${Math.abs(worst).toFixed(0)}px under the chrome. Re-measure the insets in App.tsx.`);
    return 1;
  }
  console.log(`\nClear everywhere; the tightest is ${worst.toFixed(0)}px.`);
  return 0;
}

process.exitCode = await main();
