// 通过 CDP 连接到一个已独立启动的 Chrome，抓取页面文本 + 截图。
// 用法: node grab_page.mjs <url> [outName] [--scroll]
// 依赖: playwright-core (装在 /Users/xjs/.workbuddy/binaries/node/workspace)
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire('/Users/xjs/.workbuddy/binaries/node/workspace/');
const { chromium } = require('playwright-core');

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222';
const url = process.argv[2];
const outName = process.argv[3] || 'page';
const doScroll = process.argv.includes('--scroll');
const OUT_DIR = '/Users/xjs/github/leko-ai/.workbuddy/tmp';

if (!url) {
  console.error('用法: node grab_page.mjs <url> [outName] [--scroll]');
  process.exit(1);
}

const browser = await chromium.connectOverCDP(CDP);
const ctx = browser.contexts()[0];
if (!ctx) {
  console.error('没有可用的浏览器上下文');
  process.exit(1);
}

let page = ctx.pages().find((p) => !p.isClosed() && p.url().startsWith('http'));
if (!page) page = await ctx.newPage();

await page.bringToFront().catch(() => {});
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch((e) => {
  console.error('[goto warn]', e.message);
});
await page.waitForTimeout(3500);

if (doScroll) {
  for (let i = 0; i < 6; i++) {
    await page.mouse.wheel(0, 1200);
    await page.waitForTimeout(600);
  }
  await page.waitForTimeout(1500);
}

const info = await page.evaluate(() => {
  const t = (s) => (s ? s.innerText.trim() : '');
  const title = document.title || '';
  const body = t(document.body);
  return {
    title,
    url: location.href,
    body,
  };
});

const needLogin = /请登录|立即登录|扫码登录|密码登录/.test(info.body.slice(0, 3000)) && info.body.length < 6000;

const shot = path.join(OUT_DIR, `${outName}.png`);
await page.screenshot({ path: shot, fullPage: false });

const txt = path.join(OUT_DIR, `${outName}.txt`);
fs.writeFileSync(
  txt,
  `URL: ${info.url}\nTITLE: ${info.title}\nNEED_LOGIN: ${needLogin}\n\n=== BODY ===\n${info.body}\n`,
  'utf8'
);

console.log('TITLE:', info.title);
console.log('FINAL_URL:', info.url);
console.log('NEED_LOGIN:', needLogin);
console.log('TEXT_LEN:', info.body.length);
console.log('SCREENSHOT:', shot);
console.log('TEXTFILE:', txt);
console.log('--- BODY PREVIEW (first 3000 chars) ---');
console.log(info.body.slice(0, 3000));

await browser.close(); // 只断开 CDP 连接，不关闭 Chrome 进程
