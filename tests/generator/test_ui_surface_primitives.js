const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP = path.resolve(__dirname, '../../Prisma/generator/app');
const HTML = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
const CSS = fs.readFileSync(path.join(APP, 'style.css'), 'utf8');
const JS = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');

function elementById(id) {
  const match = HTML.match(new RegExp(`<[^>]+id="${id}"[^>]*>[\\s\\S]*?<\\/button>`));
  assert.ok(match, `index.html must contain #${id}`);
  return match[0];
}

function elementBlockById(id, closingTag) {
  const match = HTML.match(new RegExp(`<[^>]+id="${id}"[^>]*>[\\s\\S]*?<\\/${closingTag}>`));
  assert.ok(match, `index.html must contain #${id}`);
  return match[0];
}

function cssRuleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = CSS.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`));
  assert.ok(match, `style.css must contain ${selector}`);
  return match[1];
}

function htmlUsesClassToken(className) {
  const attrPattern = /class="([^"]*)"/g;
  let match;
  while ((match = attrPattern.exec(HTML)) !== null) {
    if (match[1].split(/\s+/).includes(className)) return true;
  }
  return false;
}

function cssDefinesClassSelector(className) {
  const escaped = className.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(^|[\\s,{])\\.${escaped}(?=[\\s,{.:#>+~\\[]|$)`, 'm').test(CSS);
}

function surfaceWindowControlChunkFor(id) {
  const match = HTML.match(new RegExp(`<div class="surface-window-controls">[\\s\\S]*?id="${id}"[\\s\\S]*?<\\/div>`));
  assert.ok(match, `#${id} should sit inside .surface-window-controls`);
  return match[0];
}

function functionBody(signature, nextSignature) {
  const start = JS.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const end = JS.indexOf(nextSignature, start);
  assert.ok(end > start, `missing boundary after ${signature}`);
  return JS.slice(start, end);
}

test('shared surface primitive CSS aliases exist', () => {
  for (const primitive of [
    'surface-overlay',
    'surface-window',
    'surface-header',
    'surface-title',
    'surface-header-actions',
    'surface-window-controls',
    'surface-close',
    'surface-body',
    'surface-footer',
    'surface-drawer',
    'surface-menu',
    'surface-lightbox',
  ]) {
    assert.match(CSS, new RegExp(`\\.${primitive}\\b`), `style.css must define .${primitive}`);
  }
  assert.match(CSS, /--z-modal-overlay/, 'surface tiers should use z-index variables');
  assert.match(CSS, /--z-interrupt-dialog/, 'interrupt dialogs should have an explicit tier');
});

test('first migrated modals carry shared surface classes', () => {
  for (const id of [
    'settingsProfileModal',
    'settingsProfileSaveModal',
    'savedRunsModal',
    'renameSavedRunModal',
    'libraryModalBackdrop',
    'ratioDialog',
    'paletteSaveModal',
  ]) {
    const modalStart = HTML.indexOf(`id="${id}"`);
    assert.notEqual(modalStart, -1, `index.html must contain #${id}`);
    const modalChunk = HTML.slice(Math.max(0, modalStart - 120), modalStart + 1400);
    assert.match(modalChunk, /surface-overlay/, `#${id} overlay should use surface-overlay`);
    assert.match(modalChunk, /surface-window/, `#${id} shell should use surface-window`);
    assert.match(modalChunk, /surface-header/, `#${id} header should use surface-header`);
    assert.match(modalChunk, /surface-title/, `#${id} title should use surface-title`);
  }
});

test('migrated header close buttons are icon-only and accessible', () => {
  assert.match(
    CSS,
    /\.surface-close\s*\{[\s\S]*?color:\s*var\(--muted\);/,
    'shared corner-close X should default to muted dark gray, not black',
  );
  assert.match(
    CSS,
    /\.surface-window-controls\s*\{[\s\S]*?border-left:\s*1px solid color-mix\(in srgb, var\(--muted\) 22%, transparent\);/,
    'shared window controls should have a left divider before close/chrome buttons',
  );
  assert.match(
    CSS,
    /\.surface-window-controls \.surface-header-button\s*\{[\s\S]*?background:\s*transparent;[\s\S]*?border-color:\s*transparent;/,
    'window chrome buttons should blend into the header at rest',
  );
  for (const id of [
    'settingsProfileModalClose',
    'settingsProfileSaveModalClose',
    'savedRunsCloseBtn',
    'renameSavedRunCancel',
    'libraryModalClose',
    'closeSettingsDrawer',
    'closeDetailDrawer',
    'ratioDialogClose',
    'paletteSaveModalClose',
    'printerConfigClose',
  ]) {
    const button = elementById(id);
    assert.match(button, /surface-close/, `#${id} should use surface-close`);
    assert.match(button, /aria-label="Close /, `#${id} needs a specific aria-label`);
    assert.match(button, /title="Close /, `#${id} needs a specific title`);
    assert.match(button, /<svg\b/, `#${id} should render the shared X icon`);
    assert.equal(/>\s*Close\s*</.test(button), false, `#${id} must not expose a text Close label in the header`);
    assert.match(surfaceWindowControlChunkFor(id), /surface-close/, `#${id} should be grouped in the shared window-control chrome`);
  }
});

test('detail drawer uses shared header and close primitives while retaining blocking overlay', () => {
  const drawerStart = HTML.indexOf('id="detailDrawer"');
  assert.notEqual(drawerStart, -1, 'index.html must contain #detailDrawer');
  const drawerChunk = HTML.slice(Math.max(0, drawerStart - 120), drawerStart + 1200);
  assert.match(drawerChunk, /surface-drawer/, 'Detail drawer should use surface-drawer');
  assert.match(drawerChunk, /surface-header/, 'Detail drawer header should use surface-header');
  assert.match(drawerChunk, /surface-title/, 'Detail drawer title should use surface-title');
  assert.match(drawerChunk, /surface-body/, 'Detail drawer body should use surface-body');
  assert.match(elementById('closeDetailDrawer'), /surface-close/, 'Detail drawer close button should be the shared icon close');
  assert.match(
    CSS,
    /\.detail-drawer\s*\{[\s\S]*?position:\s*sticky;[\s\S]*?display:\s*none;/,
    'detail drawer should retain its sticky side-panel hidden-state behavior',
  );
  assert.match(
    CSS,
    /\.detail-drawer\[aria-hidden="false"\]\s*\{[\s\S]*?display:\s*grid;/,
    'detail drawer should still show as the existing grid side panel',
  );
  assert.match(
    JS,
    /function openDetailDrawer\(title, bodyHtml\)[\s\S]*?overlay\.setAttribute\("aria-hidden", "false"\)/,
    'openDetailDrawer should continue showing the blocking overlay',
  );
  assert.match(
    JS,
    /function closeDetailDrawer\(\)[\s\S]*?overlay\.setAttribute\("aria-hidden", "true"\)/,
    'closeDetailDrawer should continue hiding the blocking overlay',
  );
});

test('settings drawer uses shared header and close primitives without changing overlay behavior', () => {
  const drawerStart = HTML.indexOf('id="settingsDrawer"');
  assert.notEqual(drawerStart, -1, 'index.html must contain #settingsDrawer');
  const drawerChunk = HTML.slice(Math.max(0, drawerStart - 120), drawerStart + 1800);
  assert.match(drawerChunk, /surface-drawer/, 'Settings drawer should use surface-drawer');
  assert.match(drawerChunk, /surface-header/, 'Settings drawer header should use surface-header');
  assert.match(drawerChunk, /surface-title/, 'Settings drawer title should use surface-title');
  assert.match(drawerChunk, /surface-body/, 'Settings drawer body should use surface-body');
  assert.match(elementById('settingsAdvancedToggle'), /view-option-toggle/, 'Advanced settings toggle should use the preview-style On/Off button primitive');
  assert.match(drawerChunk, /id="settingsAdvancedToggle"[\s\S]*?surface-window-controls[\s\S]*?id="closeSettingsDrawer"/, 'Advanced settings toggle should live in the settings header before the close control');
  assert.doesNotMatch(HTML, /id="settingsDrawerWidthToggle"/, 'obsolete drawer width toggle should be removed');
  assert.doesNotMatch(HTML, /id="settingsDrawerNav"/, 'obsolete compact drawer navigation should be removed');
  assert.equal(/settings-advanced-toggle/.test(HTML), false, 'old body checkbox wrapper should be removed');
  assert.match(JS, /toggle\.textContent = `Advanced: \$\{settingsAdvancedVisible \? "On" : "Off"\}`;/, 'Advanced header toggle should show On/Off state');
  assert.match(JS, /advancedToggle\.addEventListener\("click"/, 'Advanced header toggle should be button-click driven');
  assert.match(JS, /function panelResizeIconSvg\(expanded\)/, 'image library resize control should retain its shared icon markup');
  assert.match(elementById('closeSettingsDrawer'), /surface-close/, 'close button should be the shared icon close');
  assert.match(
    surfaceWindowControlChunkFor('closeSettingsDrawer'),
    /id="closeSettingsDrawer"/,
    'settings drawer close should remain in the shared window-control group',
  );
  assert.match(
    CSS,
    /\.settings-drawer\s*\{[\s\S]*?top:\s*14px;[\s\S]*?display:\s*flex;[\s\S]*?flex-direction:\s*column;[\s\S]*?overflow:\s*hidden;[\s\S]*?transform:\s*translateX\(100%\);/,
    'settings drawer should retain its top gap and use one bounded vertical flex body',
  );
  assert.match(
    CSS,
    /\.settings-drawer-headings\s*\{[\s\S]*?align-items:\s*flex-start;[\s\S]*?text-align:\s*left;/,
    'settings drawer title should remain left-aligned like other surface headers',
  );
  assert.match(
    JS,
    /function openSettingsDrawer\(\)[\s\S]*?drawer\.setAttribute\("aria-hidden", "false"\)/,
    'openSettingsDrawer should retain aria-hidden opening behavior',
  );
  assert.match(
    JS,
    /function closeSettingsDrawer\(\)[\s\S]*?drawer\.setAttribute\("aria-hidden", "true"\)/,
    'closeSettingsDrawer should retain aria-hidden closing behavior',
  );
});

test('active library modal follows surface overlay state contract', () => {
  const modalStart = HTML.indexOf('id="libraryModalBackdrop"');
  assert.notEqual(modalStart, -1, 'index.html must contain #libraryModalBackdrop');
  const modalChunk = HTML.slice(Math.max(0, modalStart - 180), modalStart + 1600);
  assert.match(modalChunk, /aria-hidden="true"/, 'Active Library modal should start hidden from assistive tech');
  assert.match(modalChunk, /modal-overlay/, 'Active Library should use the shared modal overlay class');
  assert.match(modalChunk, /surface-overlay/, 'Active Library should use surface-overlay');
  assert.match(modalChunk, /surface-window/, 'Active Library shell should use surface-window');
  assert.match(modalChunk, /surface-header/, 'Active Library header should use surface-header');
  assert.match(elementById('libraryFilterSelectAll'), /surface-header-action-button/, 'Active Library All button should use compact header action sizing');
  assert.match(elementById('libraryFilterDeselectAll'), /surface-header-action-button/, 'Active Library None button should use compact header action sizing');
  assert.match(
    CSS,
    /\.modal-dialog\.library-modal\s*\{[\s\S]*?width:\s*min\(920px, 92vw\);[\s\S]*?max-width:\s*min\(920px, 92vw\);/,
    'Active Library modal should out-specific the base .modal-dialog 400px cap before forcing vertical scroll',
  );
  assert.match(
    CSS,
    /\.library-modal \.surface-header-action-button\s*\{[\s\S]*?height:\s*24px;[\s\S]*?min-height:\s*24px;/,
    'Active Library header actions should be shorter than window chrome buttons',
  );
  assert.equal(
    /\.library-modal-backdrop[\s\S]*backdrop-filter/.test(CSS),
    false,
    'Active Library should not keep its old blurred backdrop',
  );
  assert.match(
    JS,
    /function openLibraryModal\(\)[\s\S]*?setAttribute\("aria-hidden", "false"\)/,
    'openLibraryModal should update aria-hidden when opening',
  );
  assert.match(
    JS,
    /function closeLibraryModal\(\)[\s\S]*?setAttribute\("aria-hidden", "true"\)/,
    'closeLibraryModal should update aria-hidden when closing',
  );
});

test('left rail Active Filaments uses a focused Manage button', () => {
  const railLibrary = elementBlockById('railLibraryBtn', 'button');
  assert.match(railLibrary, /rail-library-manage/, 'Active Filaments rail action should be scoped to the Manage button');
  assert.match(railLibrary, />Manage<\/button>/, 'Active Filaments rail action should show an explicit Manage affordance');
  assert.equal(/id="railLibraryBtn"[^>]*role="button"/.test(HTML), false, 'Active Filaments status strip should not pretend to be a button');
  assert.match(
    HTML,
    /<p class="panel-kicker">Active Filaments <span class="rail-library-count" id="railLibraryCount">\([\s\S]*?\)<\/span><\/p>[\s\S]*?id="railLibraryBtn"/,
    'Active Filaments title, compact count, and Manage button should share one line',
  );
  assert.match(
    CSS,
    /\.rail-library-count\s*\{[\s\S]*?letter-spacing:\s*0;/,
    'Active Filaments inline count should be compact and not inherit the kicker tracking',
  );
  assert.match(
    CSS,
    /\.rail-library-header\s*\{[\s\S]*?padding:\s*6px 0 8px;[\s\S]*?border-bottom:\s*1px solid/,
    'Active Filaments rail header spacing should match Palette Deck rail section rhythm',
  );
  assert.equal(cssDefinesClassSelector('rail-library-status'), false, 'Active Library should not keep a separate status body');
  assert.match(JS, /railCount\.textContent = `\(\$\{enabledCount\}\/\$\{totalEligible\}\)`;/, 'rail count should omit the word enabled');
  assert.match(
    CSS,
    /\.rail-library-manage\s*\{[\s\S]*?align-self:\s*center;/,
    'Active Library Manage button should align with the rail status text',
  );
  const manageRule = cssRuleBody('.rail-library-manage');
  assert.equal(/height:|padding:/.test(manageRule), false, 'Active Library Manage button should use normal ghost-button xxs sizing');
  assert.match(
    CSS,
    /\.rail-printer-header\s*\{[\s\S]*?margin-bottom:\s*6px;/,
    'Printer rail header should leave breathing room before the dropdown',
  );
  assert.match(
    JS,
    /railLibBtn\) railLibBtn\.addEventListener\("click", openLibraryModal\);/,
    'Active Library Manage button should open the modal on click',
  );
});

test('printer and active nozzle share a bounded sidebar row', () => {
  assert.match(
    HTML,
    /class="rail-printer-control-row"[\s\S]*?id="railPrinterSelector"[\s\S]*?id="railNozzleSelect"/,
    'printer selector/name and nozzle selector should share one row',
  );
  assert.match(CSS, /\.rail-printer-control-row\s*\{[\s\S]*?min-width:\s*0;/);
  assert.match(CSS, /\.rail-printer-selector\s*\{[\s\S]*?flex:\s*1 1 auto;[\s\S]*?min-width:\s*0;/);
  assert.match(CSS, /\.rail-printer-name\s*\{[\s\S]*?text-overflow:\s*ellipsis;[\s\S]*?white-space:\s*nowrap;/);
  assert.match(CSS, /\.rail-nozzle-select\s*\{[\s\S]*?flex:\s*0 0 66px;[\s\S]*?width:\s*66px;/);
  assert.match(JS, /setActivePrinter\(\{ active_printer_id: sel\.value \}\)[\s\S]*?await loadPrinters\(\)/);
  assert.match(JS, /setActivePrinter\(\{ active_nozzle_size: parseFloat\(nozzleSel\.value\) \}\)[\s\S]*?await loadPrinters\(\)/);
});

test('printer rail renders long names, multiple printers, and nozzle profiles without changing backend sync', async () => {
  const source = functionBody('function renderPrinterRail()', 'async function loadPrinters()');
  const render = Function(
    'printersData',
    'printerConfig',
    '$',
    'esc',
    'escAttr',
    'setActivePrinter',
    'loadPrinters',
    `${source}; return renderPrinterRail;`,
  );
  const container = { innerHTML: '' };
  const printerSelect = {
    value: 'printer-b',
    addEventListener(_event, handler) { this.change = handler; },
  };
  const nozzleSelect = { innerHTML: '', value: '0.6', disabled: false, onchange: null, title: '' };
  const $ = selector => ({
    '#railPrinterSelector': container,
    '#railPrinterSelect': printerSelect,
    '#railNozzleSelect': nozzleSelect,
  })[selector] || null;
  const calls = [];
  let reloads = 0;
  const printers = [
    {
      id: 'printer-a',
      name: 'A printer name that is intentionally far too long for the left sidebar',
      nozzle_profiles: [{ size: 0.4 }, { size: 0.6 }],
    },
  ];
  render(
    { printers, active_printer_id: 'printer-a', active_nozzle_size: 0.4 },
    { name: printers[0].name },
    $,
    value => value,
    value => value,
    async payload => calls.push(payload),
    async () => { reloads += 1; },
  )();
  assert.match(container.innerHTML, /rail-printer-name/);
  assert.match(container.innerHTML, /intentionally far too long/);
  assert.match(nozzleSelect.innerHTML, /0\.4mm/);
  assert.match(nozzleSelect.innerHTML, /0\.6mm/);
  assert.equal(nozzleSelect.disabled, false);
  await nozzleSelect.onchange();
  assert.deepEqual(calls.pop(), { active_nozzle_size: 0.6 });
  assert.equal(reloads, 1);

  printers.push({ id: 'printer-b', name: 'Second printer', nozzle_profiles: [{ size: 0.6 }] });
  render(
    { printers, active_printer_id: 'printer-a', active_nozzle_size: 0.4 },
    { name: printers[0].name },
    $,
    value => value,
    value => value,
    async payload => calls.push(payload),
    async () => { reloads += 1; },
  )();
  assert.match(container.innerHTML, /id="railPrinterSelect"/);
  assert.match(container.innerHTML, /Second printer/);
  await printerSelect.change();
  assert.deepEqual(calls.pop(), { active_printer_id: 'printer-b' });
  assert.equal(reloads, 2);

  render(
    { printers: [], active_printer_id: null, active_nozzle_size: null },
    { name: 'Stale previous printer' },
    $,
    value => value,
    value => value,
    async payload => calls.push(payload),
    async () => { reloads += 1; },
  )();
  assert.match(container.innerHTML, /No printer configured/);
  assert.doesNotMatch(container.innerHTML, /Stale previous printer/);
  assert.equal(nozzleSelect.disabled, true);
  assert.equal(nozzleSelect.innerHTML, '');
});

test('AMS preview labels the shared white base and cap slot compactly', () => {
  assert.match(JS, /class="ams-preview-base-label"/, 'AMS preview should use a reusable base/cap label class');
  assert.match(JS, /<span>BASE\/<\/span><span>CAP<\/span>/, 'AMS preview should split Base/Cap across two lines');
  assert.equal(/font-size:7px;color:#999;font-weight:700;">BASE/.test(JS), false, 'AMS preview should not keep the old inline BASE label');
  assert.match(
    CSS,
    /\.ams-preview-slot\s*\{[\s\S]*?height:\s*32px;/,
    'AMS preview slot size should remain stable while the label changes',
  );
  assert.match(
    CSS,
    /\.ams-preview-base-label\s*\{[\s\S]*?flex-direction:\s*column;[\s\S]*?font-size:\s*7px;[\s\S]*?line-height:\s*0\.95;/,
    'AMS preview base/cap label should stack inside the existing small slot',
  );
});

test('custom ratio dialog uses surface window primitives instead of inline modal styling', () => {
  const dialogStart = HTML.indexOf('id="ratioDialog"');
  assert.notEqual(dialogStart, -1, 'index.html must contain #ratioDialog');
  const dialogChunk = HTML.slice(Math.max(0, dialogStart - 120), dialogStart + 1800);
  assert.match(dialogChunk, /surface-overlay/, 'ratio dialog should use surface-overlay');
  assert.match(dialogChunk, /surface-window/, 'ratio dialog shell should use surface-window');
  assert.match(dialogChunk, /surface-header/, 'ratio dialog should use surface-header');
  assert.match(dialogChunk, /surface-body/, 'ratio dialog should use surface-body');
  assert.match(dialogChunk, /surface-footer/, 'ratio dialog should use surface-footer');
  assert.equal(/style=/.test(dialogChunk), false, 'ratio dialog should not keep inline layout styles');
  assert.match(JS, /function openRatioDialog\(\)[\s\S]*?setAttribute\("aria-hidden", "false"\)/, 'ratio dialog should open through a helper');
  assert.match(JS, /function closeRatioDialog\(\)[\s\S]*?setAttribute\("aria-hidden", "true"\)/, 'ratio dialog should close through a helper');
  assert.match(JS, /ratioDialogClose[\s\S]*?addEventListener\("click", closeRatioDialog\)/, 'ratio close X should use the shared close helper');
});

test('app interrupt dialog uses the shared surface window pattern while remaining topmost', () => {
  const dialogStart = HTML.indexOf('id="appDialog"');
  assert.notEqual(dialogStart, -1, 'index.html must contain #appDialog');
  const nextModalStart = HTML.indexOf('id="paletteSaveModal"', dialogStart);
  const dialogChunk = HTML.slice(Math.max(0, dialogStart - 120), nextModalStart);
  assert.match(dialogChunk, /surface-overlay/, 'app dialog should use surface-overlay');
  assert.match(dialogChunk, /surface-window/, 'app dialog shell should use surface-window');
  assert.match(dialogChunk, /surface-header/, 'app dialog should use surface-header');
  assert.match(dialogChunk, /surface-title/, 'app dialog title should use surface-title');
  assert.match(dialogChunk, /surface-body/, 'app dialog body should use surface-body');
  assert.match(dialogChunk, /surface-footer/, 'app dialog buttons should use surface-footer');
  assert.match(dialogChunk, /surface-window-controls/, 'app dialog close should sit in shared window controls');
  assert.match(elementById('appDialogClose'), /surface-close/, 'app dialog should use the shared icon close');
  assert.match(CSS, /#appDialog\s*\{[\s\S]*?z-index:\s*var\(--z-interrupt-dialog\)/, 'app dialog must remain on the topmost interrupt tier');
  assert.match(JS, /overlay\.onclick\s*=\s*\(e\)\s*=>\s*\{\s*if \(e\.target === overlay\) close\(false\); \};/, 'appConfirm should retain outside-click cancel');
  assert.match(JS, /overlay\.onclick\s*=\s*\(e\)\s*=>\s*\{\s*if \(e\.target === overlay\) close\(null\); \};/, 'appPrompt/appChoice should retain outside-click null close');
  assert.match(JS, /clearAllTempBtn[\s\S]*?appConfirm\([\s\S]*?title:\s*"Clear Temp Files"/, 'Clear Temp Files should title the shared confirm dialog');
});

test('palette save dialog uses the shared modal window pattern instead of the app interrupt prompt', () => {
  const modalStart = HTML.indexOf('id="paletteSaveModal"');
  assert.notEqual(modalStart, -1, 'index.html must contain #paletteSaveModal');
  const modalChunk = HTML.slice(Math.max(0, modalStart - 120), modalStart + 1600);
  assert.match(modalChunk, /surface-overlay/, 'palette save modal should use surface-overlay');
  assert.match(modalChunk, /surface-window/, 'palette save shell should use surface-window');
  assert.match(modalChunk, /surface-header/, 'palette save modal should use surface-header');
  assert.match(modalChunk, /surface-title/, 'palette save title should use surface-title');
  assert.match(modalChunk, /surface-body/, 'palette save body should use surface-body');
  assert.match(modalChunk, /surface-footer/, 'palette save actions should use surface-footer');
  assert.match(elementById('paletteSaveModalClose'), /surface-close/, 'palette save close should be the shared icon close');
  assert.match(JS, /function showPaletteSaveModal\(/, 'palette saves should have a dedicated modal helper');
  assert.match(JS, /const alias = await showPaletteSaveModal\(card\.name\)/, 'rail palette Save should use the dedicated modal');
  assert.equal(/const alias = await appPrompt\("Palette name:"/.test(JS), false, 'rail palette Save should not use the headerless app prompt');
});

test('load palette popover uses the shared surface menu primitive', () => {
  assert.match(JS, /pop\.className\s*=\s*"load-palette-popover surface-menu"/, 'load palette popover should use surface-menu');
  assert.match(JS, /class="load-palette-item surface-menu-item"/, 'load palette rows should use surface-menu-item');
  const popoverRule = cssRuleBody('.load-palette-popover');
  assert.equal(/box-shadow/.test(popoverRule), false, 'popover should not keep local shadow styling');
  assert.equal(/border:\s*1px/.test(popoverRule), false, 'popover should not keep local border styling');
  assert.equal(/\.load-palette-item:hover/.test(CSS), false, 'popover item hover should come from surface-menu-item');
});

test('fullscreen lightbox uses shared lightbox hooks without becoming a modal window', () => {
  const lightboxStart = HTML.indexOf('id="compLightbox"');
  assert.notEqual(lightboxStart, -1, 'index.html must contain #compLightbox');
  const lightboxChunk = HTML.slice(Math.max(0, lightboxStart - 120), lightboxStart + 700);
  assert.match(lightboxChunk, /surface-lightbox/, 'comparison lightbox should use surface-lightbox');
  assert.match(lightboxChunk, /surface-lightbox-close/, 'comparison lightbox close should use surface-lightbox-close');
  assert.match(lightboxChunk, /aria-label="Close lightbox"/, 'lightbox close needs an accessible label');
  assert.match(lightboxChunk, /<svg\b/, 'lightbox close should use the shared X icon');
  assert.equal(/surface-window/.test(lightboxChunk), false, 'fullscreen lightbox should not become a white modal window');
  assert.match(CSS, /\.surface-lightbox\s*\{|\s*\.surface-lightbox\s*,/, 'style.css should define surface-lightbox');
  assert.match(CSS, /\.surface-lightbox-close/, 'style.css should define surface-lightbox-close');
  assert.match(JS, /comp-lightbox-topbar surface-lightbox-topbar/, 'solve lightbox topbar should carry the shared topbar hook');
});

test('printer configuration page uses shared window header primitives without changing page overlay type', () => {
  const pageStart = HTML.indexOf('id="printerConfigPage"');
  assert.notEqual(pageStart, -1, 'index.html must contain #printerConfigPage');
  const pageChunk = HTML.slice(Math.max(0, pageStart - 120), pageStart + 1400);
  assert.match(pageChunk, /pc-card surface-window/, 'printer config card should use surface-window');
  assert.match(pageChunk, /pc-toolbar window-header surface-header/, 'printer config header should use surface-header');
  assert.match(pageChunk, /surface-title pc-toolbar-title/, 'printer config title should use surface-title');
  assert.match(pageChunk, /window-header__actions surface-header-actions/, 'printer config header actions should use surface-header-actions');
  assert.match(elementById('printerConfigClose'), /surface-close/, 'printer config close should use the shared close primitive');
  assert.equal(/id="printerConfigPage"[^>]*surface-overlay/.test(pageChunk), false, 'printer config page should remain a local page overlay, not fixed modal-overlay');
  assert.equal(/pc-window-close/.test(HTML + CSS), false, 'printer config should not keep custom close-button styling hooks');
});

test('standalone X icon controls render SVGs instead of text glyphs', () => {
  assert.match(JS, /function xIconSvg\([^)]*\)[\s\S]*?<svg/, 'app.js should expose a reusable standalone X SVG helper');
  assert.match(CSS, /\.icon-x\b/, 'standalone X SVGs should have shared sizing');

  for (const className of [
    'nz-delete',
    'ams-slot-remove',
    'load-palette-delete',
    'deck-delete-btn',
    'rail-deck-remove',
    'solve-run-delete-btn',
  ]) {
    const firstUse = JS.indexOf(className);
    assert.notEqual(firstUse, -1, `app.js should contain .${className}`);
    const snippet = JS.slice(Math.max(0, firstUse - 240), firstUse + 520);
    assert.match(snippet, /xIconSvg\(\)/, `.${className} should render xIconSvg()`);
    assert.equal(/&times;|>\s*×\s*<|\\u00d7/.test(snippet), false, `.${className} should not render a text X glyph`);
  }

  assert.equal(/textContent\s*=\s*["']\\u00d7["']/.test(JS), false, 'standalone X reset paths should restore SVGs, not unicode text');
  assert.equal(/textContent\s*=\s*["']×["']/.test(JS), false, 'standalone X reset paths should restore SVGs, not unicode text');
});

test('legacy modal sheet primitives are not kept as a parallel surface system', () => {
  assert.equal(cssDefinesClassSelector('modal-sheet'), false, 'use surface-window/modal-dialog-window instead of modal-sheet');
  assert.equal(cssDefinesClassSelector('modal-header'), false, 'use surface-header/modal-dialog-header instead of modal-header');
  assert.equal(cssDefinesClassSelector('modal-footer'), false, 'use surface-footer/modal-dialog-footer instead of modal-footer');
  assert.equal(htmlUsesClassToken('modal-sheet'), false, 'index.html should not use modal-sheet');
  assert.equal(htmlUsesClassToken('modal-header'), false, 'index.html should not use modal-header');
  assert.equal(htmlUsesClassToken('modal-footer'), false, 'index.html should not use modal-footer');
});
