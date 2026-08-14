/**
 * Screenshots of the running app, with real mouse drags between them.
 *
 * Usage: node shots.mjs <name> [x1,y1>x2,y2 ...]
 * Each drag is played in small steps, the way a hand moves a card, so the
 * routing settles the way it does on screen rather than teleporting.
 *
 * Not part of the app: a throwaway rig for the link-routing work.
 */

import { chromium } from 'playwright-core';

const CHROME = `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`;
const URL = 'http://localhost:5173/?mock=1';

const [name, ...drags] = process.argv.slice(2);
if (!name) throw new Error('usage: node shots.mjs <name> [x1,y1>x2,y2 ...]');

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
page.on('pageerror', (e) => console.error('page error:', e.message));
await page.goto(URL, { waitUntil: 'networkidle' });
await page.waitForTimeout(2500);

let shot = 0;
const capture = async () => {
  const file = `${name}-${shot}.png`;
  await page.screenshot({ path: file });
  console.log(`shot ${file}`);
  shot += 1;
};

await capture();

for (const drag of drags) {
  // Fresh page per drag: a release springs the card back but leaves the view
  // where the last drag left it, and then the next drag grabs empty canvas.
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
  const [from, to] = drag.split('>');
  const [x1, y1] = from.split(',').map(Number);
  const [x2, y2] = to.split(',').map(Number);
  await page.mouse.move(x1, y1);
  await page.mouse.down();
  for (let i = 1; i <= 30; i += 1) {
    await page.mouse.move(x1 + ((x2 - x1) * i) / 30, y1 + ((y2 - y1) * i) / 30);
    await page.waitForTimeout(16);
  }
  // Shot with the card still held: released, it springs back to its seat, so
  // this is the only way to see the picture at the position asked for.
  await page.waitForTimeout(1200);
  await capture();
  await page.mouse.up();
  await page.waitForTimeout(1500);
}

await browser.close();
