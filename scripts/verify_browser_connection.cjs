/** Real Chromium recovery proof, with a read-only publication comparison. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const base = process.env.BROWSER_QA_URL || 'http://127.0.0.1:8002';
const origin = new URL(base).origin;
const output = path.resolve(process.env.BROWSER_QA_OUTPUT || '.local/browser-qa/recovery');
const compareVersions = process.argv.includes('--publication');
const report = { status: 'running', base, screenshots: [], viewports: [], pageErrors: [], failedResponses: [], blockedRequests: [] };
const readPosts = new Set([
  '/playground/session', '/v1/knowledge/graph:query', '/v1/knowledge/graph:evidence',
  '/v1/knowledge/sources:query', '/v1/knowledge/sources:read',
  '/v1/knowledge/publications:compare', '/v1/knowledge/quality/reviews:query',
]);
const readGets = /^\/v1\/(?:ontologies|knowledge\/(?:construction-jobs|review-queue|publication-candidates|publications|entity-resolution|review-assessments|property-assignment|publication-inventory|documents|quality))(?:\/|$)/;

(async () => {
  assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(base).hostname));
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  report.browser = browser.version();
  report.playwright = require('playwright/package.json').version;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const bootstrapResponse = await context.request.get(base + '/playground/bootstrap');
    assert.ok(bootstrapResponse.ok());
    const bootstrap = await bootstrapResponse.json();
    assert.equal(bootstrap.data_scope, 'pump-only');
    report.dataScope = bootstrap.data_scope;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      const code = url.pathname === '/industrial' || url.pathname.startsWith('/industrial/assets/');
      const allowedRead = ['GET', 'HEAD'].includes(request.method()) && (
        code || url.pathname === '/playground/bootstrap' || url.pathname === '/favicon.ico' ||
        /^\/playground\/(?:assets\/)?industrial-demo-v1\//.test(url.pathname) || readGets.test(url.pathname)
      );
      if (url.origin !== origin || !(allowedRead || request.method() === 'POST' && readPosts.has(url.pathname))) {
        report.blockedRequests.push({ method: request.method(), path: url.pathname });
        return route.abort('blockedbyclient');
      }
      await route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror', error => report.pageErrors.push(error.message));
    page.on('response', response => {
      if (response.status() >= 400) report.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
    });
    const initialGraph = page.waitForResponse(r => new URL(r.url()).pathname === '/v1/knowledge/graph:query');
    await page.goto(base + '/industrial');
    assert.ok((await initialGraph).ok(), 'real workbench graph read must succeed');
    await page.locator('.graph-toolbar').waitFor({ state: 'visible' });
    if (compareVersions) {
      await page.locator('[data-panel="maintenance"]').click();
      await page.locator('[data-flow-target="maintenance-history"]').click();
      await page.locator('[data-rollback-target]').waitFor({ state: 'visible' });
      await page.waitForFunction(() => document.querySelector('[data-rollback-target]')?.options.length > 1);
      for (const viewport of [{ width: 1280, height: 800 }, { width: 1440, height: 1000 }, { width: 1920, height: 1080 }]) {
        await page.setViewportSize(viewport);
        await page.evaluate(() => window.scrollTo(0, 0));
        const select = page.locator('[data-rollback-target]');
        await select.selectOption({ index: 1 });
        const compared = page.waitForResponse(r => new URL(r.url()).pathname === '/v1/knowledge/publications:compare');
        await page.locator('[data-open-rollback]').click();
        assert.ok((await compared).ok(), 'real publication comparison must succeed');
        const dialog = page.locator('dialog.kb-evidence[aria-label="知识维护"]');
        await dialog.waitFor({ state: 'visible' });
        await dialog.getByText('正式检索的文档范围', { exact: false }).waitFor();
        await dialog.getByText('实体、关系与属性', { exact: false }).waitFor();
        const metrics = await page.evaluate(() => {
          const dialog = document.querySelector('dialog[open]');
          const rect = dialog.getBoundingClientRect();
          return { width: innerWidth, height: innerHeight, pageWidth: document.documentElement.scrollWidth,
            dialog: { x: rect.x, y: rect.y, right: rect.right, bottom: rect.bottom } };
        });
        assert.ok(metrics.pageWidth <= metrics.width, 'no horizontal page overflow');
        assert.ok(metrics.dialog.x >= 0 && metrics.dialog.right <= metrics.width, 'comparison fits desktop');
        assert.ok(metrics.dialog.y >= 0 && metrics.dialog.bottom <= metrics.height, 'comparison fits height');
        const filename = `comparison-${viewport.width}.png`;
        await page.screenshot({ path: path.join(output, filename) });
        report.screenshots.push(filename);
        report.viewports.push(metrics);
        await dialog.locator('[data-close]').scrollIntoViewIfNeeded();
        const footer = `comparison-actions-${viewport.width}.png`;
        await page.screenshot({ path: path.join(output, footer) });
        report.screenshots.push(footer);
        await dialog.locator('[data-close]').click();
      }
      report.comparisonVerified = true;
    } else {
      for (const viewport of [{ width: 1280, height: 800 }, { width: 1440, height: 1000 }, { width: 1920, height: 1080 }]) {
        await page.setViewportSize(viewport);
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const filename = `workbench-${viewport.width}.png`;
        await page.screenshot({ path: path.join(output, filename) });
        report.screenshots.push(filename);
        report.viewports.push(viewport);
      }
    }
    report.pageTitle = await page.title();
    report.knowledgeWrites = 0;
    assert.deepEqual(report.pageErrors, []);
    assert.deepEqual(report.failedResponses, []);
    assert.deepEqual(report.blockedRequests, []);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.error = error.message;
    throw error;
  } finally {
    await browser.close();
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ status: report.status, browser: report.browser, output, screenshots: report.screenshots }));
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
