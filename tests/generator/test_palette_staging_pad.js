const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);
const INDEX_HTML = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/index.html'),
  'utf8',
);
const STYLE_CSS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/style.css'),
  'utf8',
);

test('staging pad has its own state array, distinct from the persistent deck', () => {
  assert.ok(/let stagingDeck\s*=\s*\[\]/.test(APP_JS), 'stagingDeck state must exist');
  assert.ok(/let deck\s*=\s*\[\]/.test(APP_JS), 'persistent deck state must remain');
});

test('palette creation uses local Auto-Suggest and Manual tabs instead of vertical accordions', () => {
  assert.match(
    INDEX_HTML,
    /class="creation-mode-tabs segmented-control"[\s\S]*?>Auto-Suggest<\/button>[\s\S]*?>Manual<\/button>/,
    'Palette tab should use a local Auto-Suggest | Manual segmented control',
  );
  assert.equal(
    /creation-panel-header" data-panel="(?:auto|manual)"/.test(INDEX_HTML),
    false,
    'Auto-Suggest and Manual should not use collapsible panel headers',
  );
  assert.ok(
    APP_JS.includes('$$(".creation-mode-tabs .segmented-btn[data-panel]").forEach'),
    'mode switching should be bound to the local tab buttons',
  );
  assert.match(
    APP_JS,
    /layout\.classList\.toggle\("is-manual-mode", !isAuto\);[\s\S]*deckPanel\.hidden = !isAuto;[\s\S]*manualPalettePanel\.hidden = isAuto;/,
    'Manual mode should swap the auto-only Suggested Palettes column for the Manual Palette panel',
  );
  assert.match(
    INDEX_HTML,
    /id="creationDeckPanel"[\s\S]*?Suggested Palettes[\s\S]*?id="manualPalettePanel"[\s\S]*?Manual Palette/,
    'Auto-Suggest and Manual should each have a right-side holding panel',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-deck\[hidden\]\s*\{\s*display:\s*none;\s*\}/,
    'hidden right-side palette panels should override the flex display on .creation-deck',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-mode-tabs\s*\{[\s\S]*?align-self:\s*flex-start;[\s\S]*?width:\s*max-content;/,
    'Palette mode tabs should self-size like the Preview tab strip instead of spanning the whole shell',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-mode-tabs \.segmented-btn\s*\{[\s\S]*?min-width:\s*0;[\s\S]*?padding:\s*4px 12px;/,
    'Palette mode tab buttons should fit their labels rather than reserve wide empty space',
  );
});

test('palette suggestion settings are renamed, collapsed, and scoped to Auto-Suggest', () => {
  const autoPanelStart = INDEX_HTML.indexOf('id="panelAutoSuggest"');
  const manualPanelStart = INDEX_HTML.indexOf('id="panelManualBuilder"');
  assert.notEqual(autoPanelStart, -1, 'Auto-Suggest panel must exist');
  assert.notEqual(manualPanelStart, -1, 'Manual panel must exist');
  const autoPanel = INDEX_HTML.slice(autoPanelStart, manualPanelStart);
  assert.match(autoPanel, /id="creationSettingsShell"/, 'Palette suggestion settings should live inside Auto-Suggest');
  assert.match(autoPanel, />Palette Suggestion Settings<\/span>/, 'settings title should use the new concise name');
  assert.equal(/Palette suggestion and comparison tuning/.test(INDEX_HTML), false, 'old settings description should be removed');
  assert.equal(/Deck Generation Settings/.test(INDEX_HTML), false, 'old Deck Generation Settings label should be removed');
  assert.match(
    STYLE_CSS,
    /\.creation-settings-summary\s*\{[\s\S]*?justify-content:\s*flex-start;[\s\S]*?gap:\s*0;/,
    'palette suggestion settings title should remain left-aligned after removing the old right-side subtitle',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-settings-summary-title\s*\{[\s\S]*?color:\s*var\(--muted\);[\s\S]*?letter-spacing:\s*0\.08em;/,
    'palette suggestion settings title should match the muted uppercase header style',
  );
});

test('Auto-Suggest filament selection controls live in the panel header', () => {
  assert.match(
    INDEX_HTML,
    /<div class="creation-panel-header auto-suggest-header">[\s\S]*?Select Filaments for Auto-Suggest[\s\S]*?id="candidateCountChip"[\s\S]*?id="candidateSelectNone"[\s\S]*?id="candidateSelectAll"[\s\S]*?<\/div>/,
    'Auto-Suggest title, count, and None/All actions should share the panel header',
  );
  assert.equal(
    /<div class="builder-half-label">\s*<span>Library<\/span>/.test(INDEX_HTML),
    false,
    'filament list bodies should not keep redundant Library labels',
  );
  assert.match(
    INDEX_HTML,
    /<div class="creation-panel-header manual-palette-header">[\s\S]*?Select Filaments for Palette[\s\S]*?id="manualLibraryCountChip"[\s\S]*?<\/div>/,
    'Manual mode should have its own panel header with the available filament count',
  );
  assert.match(
    INDEX_HTML,
    /id="manualPalettePanel"[\s\S]*?<span class="panel-kicker">Manual Palette<\/span>[\s\S]*?id="manualAmsPane"/,
    'the manual AMS builder should live in its own Manual Palette side panel',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-panel-single\s*\{[\s\S]*?display:\s*flex;[\s\S]*?min-height:\s*320px;/,
    'Manual filament selection should no longer reserve the old embedded AMS column',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-layout\s*\{[\s\S]*?--palette-side-panel-width:\s*220px;[\s\S]*?--palette-auto-controls-width:\s*200px;[\s\S]*?--palette-panel-gap:\s*12px;/,
    'Palette layout should name the shared side-panel, auto-controls, and gap widths',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-panel-split\s*\{[\s\S]*?grid-template-columns:\s*1fr var\(--palette-auto-controls-width\);/,
    'Auto-Suggest filament library should reserve the named controls width',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-layout\.is-manual-mode \.creation-modes\s*\{[\s\S]*?flex:\s*0 0 calc\(100% - var\(--palette-side-panel-width\) - var\(--palette-auto-controls-width\) - var\(--palette-panel-gap\)\);/,
    'Manual mode should size the filament selector to match Auto-Suggest library width without widening the Manual Palette panel',
  );
  assert.equal(
    /\.creation-layout\.is-manual-mode \.manual-palette-panel\s*\{[\s\S]*?margin-left:\s*auto;/.test(STYLE_CSS),
    false,
    'Manual Palette should sit beside the selector with the normal panel gap, not be pushed across dead space',
  );
  assert.match(
    STYLE_CSS,
    /\.manual-palette-panel\s*\{[\s\S]*?width:\s*var\(--palette-side-panel-width\);/,
    'Manual Palette panel should keep the compact side-panel width',
  );
  assert.match(
    STYLE_CSS,
    /\.manual-palette-panel \.ams-slots,\s*[\r\n]+\.manual-palette-panel \.ams-status,\s*[\r\n]+\.manual-palette-action\s*\{[\s\S]*?width:\s*calc\(var\(--palette-side-panel-width\) - 20px\);/,
    'Manual palette slots and actions should retain the compact side-panel content width',
  );
  assert.equal(
    /id="mintPaletteBtn"[^>]*style="width:100%"/.test(INDEX_HTML),
    false,
    'Manual Add to Deck should not keep inline full-width sizing',
  );
  assert.match(
    STYLE_CSS,
    /\.auto-suggest-header,\s*[\r\n]+\.manual-palette-header\s*\{[\s\S]*?cursor:\s*default;/,
    'Auto-Suggest and Manual panel headers should share the non-collapsible header treatment',
  );
  assert.match(
    INDEX_HTML,
    /id="manualLibraryGrid"/,
    'Manual filament grid should remain after removing the redundant Library label',
  );
  assert.match(
    STYLE_CSS,
    /\.auto-suggest-count,\s*[\r\n]+\.manual-palette-count\s*\{[\s\S]*?min-width:\s*0;[\s\S]*?padding:\s*2px 9px;/,
    'Auto-Suggest and Manual count pills should be snug around the numbers',
  );
  assert.match(
    STYLE_CSS,
    /\.creation-header-actions\s*\{[\s\S]*?gap:\s*5px;[\s\S]*?margin-left:\s*10px;/,
    'Auto-Suggest header actions should keep breathing room from the count pill',
  );
});

test('suggestions land in the staging pad, while manual builds go straight to the persistent deck', () => {
  // Suggestion helper + legacy composer staging target stagingDeck.
  const suggestPushes = APP_JS.split('stagingDeck.push(card)').length - 1;
  assert.equal(suggestPushes, 2, `suggestions and legacy composer staging should be the only staging pushes (found ${suggestPushes})`);
  // The creation-tab deck container renders from stagingDeck.
  assert.ok(
    APP_JS.includes('container.innerHTML = stagingDeck.map('),
    '#deckCards (creation tab) must render from stagingDeck',
  );
  assert.match(
    APP_JS,
    /function mintPaletteToDeck\(\)\s*\{[\s\S]*?if \(creationMode === "manual"\) \{[\s\S]*?deck\.push\(card\);[\s\S]*?if \(!activeDeckId\) activeDeckId = card\.id;[\s\S]*?manualSlots = \[\];/,
    'manual Add to Deck should add directly to the persistent deck and activate only when no deck card is active',
  );
});

test('the persistent rail deck renders from `deck` and owns active/Save', () => {
  assert.ok(APP_JS.includes('list.innerHTML = deck.map('), 'rail deck renders from the persistent deck');
  // Save moved onto the rail (persistent) cards.
  assert.ok(APP_JS.includes('rail-deck-save'), 'persistent rail cards must expose Save');
  // Solve/active reads the persistent deck only.
  assert.ok(
    /function getActivePalette\(\)\s*\{[^}]*deck\.find\(d => d\.id === activeDeckId\)/.test(APP_JS),
    'the active palette (solve source) must come from the persistent deck',
  );
});

test('rail deck Save and Saved controls keep a stable header footprint', () => {
  assert.ok(
    APP_JS.includes('card.saved ? `<span class="rail-deck-tag is-saved">Saved</span>` : ""'),
    'saved rail cards should render the Saved badge in the action slot',
  );
  assert.ok(
    APP_JS.includes('${!card.saved ? `<button class="ghost-button xxs rail-deck-save"'),
    'unsaved rail cards should render Save in the same action slot',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-deck-save,\s*[\r\n]+\.rail-deck-tag,\s*[\r\n]+\.rail-deck-remove\s*\{[\s\S]*?display:\s*inline-flex;[\s\S]*?height:\s*18px;[\s\S]*?font-size:\s*9px;/,
    'Save, Saved, and X rail controls should share height and type sizing',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-deck-save,\s*[\r\n]+\.rail-deck-tag\s*\{[\s\S]*?min-width:\s*34px;[\s\S]*?justify-content:\s*center;/,
    'Save button and Saved badge should occupy a consistent pill footprint',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-deck-remove\s*\{[\s\S]*?width:\s*18px;[\s\S]*?min-width:\s*18px;[\s\S]*?color:\s*var\(--muted\);/,
    'rail X button should share the same square action height with a muted icon color',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-deck-remove:hover\s*\{[\s\S]*?background:\s*color-mix\(in srgb, #c04040 20%, transparent\);[\s\S]*?color:\s*#c04040;/,
    'rail X button should mirror solve-run delete red hover feedback',
  );
  assert.match(
    STYLE_CSS,
    /\.icon-x path\s*\{[\s\S]*?stroke-width:\s*1\.25;/,
    'standalone X SVGs should use the shared lighter stroke weight',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-deck-card-header\s*\{[\s\S]*?align-items:\s*center;[\s\S]*?height:\s*22px;[\s\S]*?min-height:\s*22px;[\s\S]*?padding:\s*0 5px;[\s\S]*?box-sizing:\s*border-box;/,
    'rail card headers should vertically center 18px action controls with balanced top/bottom gaps',
  );
  assert.match(
    STYLE_CSS,
    /\.deck-support-tray\s*\{[\s\S]*?grid-template-columns:\s*auto;/,
    'shared Base/Cap support tray should size to the single forced shared support filament',
  );
});

test('Palette Deck owns scrolling without shrinking its cards', () => {
  const railCardCss = STYLE_CSS.match(/\.rail-deck-card\s*\{(?<body>[\s\S]*?)}/).groups.body;
  const railListCss = STYLE_CSS.match(/\.rail-deck-list\s*\{(?<body>[\s\S]*?)}/).groups.body;
  assert.match(railListCss, /overflow-y:\s*auto;/, 'Palette Deck list should own vertical scrolling');
  assert.match(railCardCss, /flex:\s*0 0 auto;/, 'Palette Deck cards must retain their natural height');
  assert.match(railCardCss, /min-width:\s*0;/, 'Palette Deck cards must remain horizontally shrinkable');
});

test('recommendation is metadata, not part of a generated palette name', () => {
  assert.equal(
    /suggested \$\{nextDeckNum\+\+}\$\{isRecommended/.test(APP_JS),
    false,
    'new suggested palette names should not encode recommendation state',
  );
  assert.match(APP_JS, /\{ recommended: isRecommended \}/, 'suggestion cards should retain recommendation metadata');
  assert.doesNotMatch(APP_JS, /palette-recommendation-tag|Recommended palette:/, 'dE-based recommendation badges should not be shown');
  assert.doesNotMatch(STYLE_CSS, /\.palette-recommendation-tag\s*\{/, 'retired recommendation badge styles should be removed');
});

test('long Palette Deck titles yield to a fixed action cluster', () => {
  const titlebarCss = STYLE_CSS.match(/\.rail-deck-card-titlebar\s*\{(?<body>[\s\S]*?)}/).groups.body;
  const titleCss = STYLE_CSS.match(/\.rail-deck-card-title\s*\{(?<body>[\s\S]*?)}/).groups.body;
  const actionsCss = STYLE_CSS.match(/\.rail-deck-card-actions\s*\{(?<body>[\s\S]*?)}/).groups.body;
  assert.match(titlebarCss, /min-width:\s*0;/, 'titlebar must be allowed to shrink');
  assert.match(titleCss, /flex:\s*1 1 auto;/, 'title text must yield available width');
  assert.match(titleCss, /text-overflow:\s*ellipsis;/, 'long title text should truncate');
  assert.match(actionsCss, /flex-shrink:\s*0;/, 'Save and remove actions must remain visible');
  assert.match(
    APP_JS,
    /rail-deck-card-title" title="\$\{escAttr\(card\.name\)\}"/,
    'the full title should remain available on hover',
  );
  assert.match(APP_JS, /function escAttr\(str\)/, 'attribute values should use quote-safe escaping');
});

test('rail deck hover preview uses compact shared styling without repeated manufacturer rows', () => {
  assert.ok(APP_JS.includes('function buildRailDeckHoverPreview(card)'), 'rail hover preview renderer must exist');
  assert.ok(APP_JS.includes('function railHoverFilamentLabel('), 'rail hover labels should dedupe manufacturer names for tooltips');
  assert.ok(APP_JS.includes('let railDeckHoverPendingCardId = null;'), 'rail hover should track pending card previews');
  const hoverBlock = APP_JS.slice(
    APP_JS.indexOf('function railHoverFilamentLabel('),
    APP_JS.indexOf('function positionRailDeckHoverPreview'),
  );
  assert.ok(!hoverBlock.includes('rail-hover-filament-brand'), 'hover preview should not render a separate manufacturer line');
  assert.match(
    hoverBlock,
    /colorName\.toLocaleLowerCase\(\)\.startsWith\(manufacturer\.toLocaleLowerCase\(\)\)/,
    'tooltip labels should avoid manufacturer duplication when color_name already includes it',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-deck-hover-preview\s*\{[\s\S]*?border-radius:\s*8px;[\s\S]*?background:\s*var\(--panel\);[\s\S]*?box-shadow:\s*var\(--shadow\);/,
    'hover preview should use the shared restrained surface shell',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-hover-head\s*\{[\s\S]*?background:\s*#d2dae0;/,
    'hover preview should use the shared title-bar color',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-hover-title\s*\{[\s\S]*?font-family:\s*"Segoe UI", Arial, sans-serif;[\s\S]*?font-size:\s*12px;[\s\S]*?letter-spacing:\s*0\.04em;[\s\S]*?text-transform:\s*uppercase;[\s\S]*?color:\s*var\(--muted\);/,
    'hover preview title should use the shared surface title typography',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-hover-filament-list\s*\{[\s\S]*?gap:\s*3px;[\s\S]*?padding:\s*0 6px;/,
    'hover filament rows should be tightly spaced',
  );
  assert.match(
    STYLE_CSS,
    /\.rail-hover-filament-row\s*\{[\s\S]*?min-height:\s*22px;[\s\S]*?padding:\s*3px 6px;[\s\S]*?border-radius:\s*3px;[\s\S]*?background:\s*#fff;[\s\S]*?border:\s*1px solid var\(--line\);/,
    'hover filament rows should look like compact white content rows',
  );
});

test('rail deck hover preview opens from card body but not action buttons', () => {
  const renderBlock = APP_JS.slice(
    APP_JS.indexOf('list.querySelectorAll(".rail-deck-card")'),
    APP_JS.indexOf('list.querySelectorAll(".rail-deck-save")'),
  );
  assert.ok(
    renderBlock.includes('el.addEventListener("mousemove", (e) => handleRailDeckCardHoverMove(el, e));'),
    'rail card hover should be based on pointer location inside the card',
  );
  assert.ok(!renderBlock.includes('mouseenter'), 'rail card hover should not blindly trigger from card mouseenter');

  const hoverBehaviorSource = APP_JS.slice(
    APP_JS.indexOf('function scheduleRailDeckHoverPreview('),
    APP_JS.indexOf('function buildDeckSupportChipsHtml'),
  );
  assert.ok(
    hoverBehaviorSource.includes('.rail-deck-card-actions button'),
    'hover preview should be blocked over Save/remove buttons',
  );
  assert.ok(
    /function handleRailDeckCardHoverMove\(cardEl, event\)\s*\{[\s\S]*?hideRailDeckHoverPreview\(\);[\s\S]*?scheduleRailDeckHoverPreview\(cardEl\.dataset\.cardId, cardEl\);/.test(hoverBehaviorSource),
    'card body movement should schedule preview, while action movement hides it',
  );
  assert.ok(
    /function scheduleRailDeckHoverPreview\(cardId, anchorEl\)\s*\{[\s\S]*?railDeckHoverPendingCardId === cardId[\s\S]*?return;/.test(hoverBehaviorSource),
    'mousemove should not repeatedly restart the same pending hover preview timer',
  );
});

test('Promote moves a staged card into the persistent deck', () => {
  assert.ok(APP_JS.includes('function promoteStagedCard('), 'promoteStagedCard must exist');
  assert.ok(
    /function promoteStagedCard\(cardId\)\s*\{[\s\S]*?stagingDeck\.splice\(idx, 1\)[\s\S]*?deck\.push\(card\)/.test(APP_JS),
    'promote must remove from stagingDeck and push into the persistent deck',
  );
  assert.ok(APP_JS.includes('deck-promote-btn'), 'staged cards must render a Promote control');
});

test('staged cards have no Save and are not selectable/active', () => {
  assert.ok(!APP_JS.includes('deck-save-btn'), 'the old staging-card Save button must be gone');
  // The staging render block must not bind a card-body set-active click.
  const stagingBlock = APP_JS.slice(
    APP_JS.indexOf('container.innerHTML = stagingDeck.map('),
    APP_JS.indexOf('function renderRailDeck'),
  );
  assert.ok(
    !stagingBlock.includes('setActiveDeckCard'),
    'staging cards must not wire set-active',
  );
});

test('each deck has its own scoped Clear; the redundant staging Load is removed', () => {
  // Staging Clear clears stagingDeck only.
  assert.ok(/if \(stagingDeck\.length === 0\) return;/.test(APP_JS), 'staging Clear guards on stagingDeck');
  assert.ok(
    /stagingDeck = \[\];\s*\n\s*suggestCapacityNote = "";\s*\n\s*renderCreationTab\(\);/.test(APP_JS),
    'staging Clear empties stagingDeck and refreshes the staging panel only',
  );
  // New persistent rail Clear.
  assert.ok(INDEX_HTML.includes('id="railClearDeckBtn"'), 'rail must have a Clear button');
  assert.ok(APP_JS.includes('const railClearDeckBtn = $("#railClearDeckBtn")'), 'rail Clear must be wired');
  // Redundant staging Load button removed; rail Load remains.
  assert.ok(!INDEX_HTML.includes('id="loadPaletteBtn"'), 'redundant staging Load button must be removed');
  assert.ok(INDEX_HTML.includes('id="railLoadPaletteBtn"'), 'rail Load button remains');
});

test('rail deck Load and Clear are clustered as a right-aligned action group', () => {
  const railSectionHeaderCss = STYLE_CSS.match(/\.rail-section-header\s*{(?<body>[\s\S]*?)}/).groups.body;
  assert.ok(
    /<div class="rail-section-actions">\s*<button class="ghost-button xxs" id="railLoadPaletteBtn"[\s\S]*?<button class="ghost-button xxs" id="railClearDeckBtn"/.test(INDEX_HTML),
    'rail Load and Clear must share one action group',
  );
  assert.ok(/\.rail-section-actions\s*{[\s\S]*?margin-left:\s*auto;/.test(STYLE_CSS), 'rail action group must align right');
  assert.ok(!railSectionHeaderCss.includes('justify-content'), 'rail section header must not distribute title/actions across the full width');
});

test('staging panel is titled "Suggested Palettes"; persistent rail keeps "Palette Deck"', () => {
  assert.ok(INDEX_HTML.includes('>Suggested Palettes<'), 'staging panel title must read "Suggested Palettes"');
  assert.ok(INDEX_HTML.includes('>Palette Deck<'), 'persistent rail deck keeps its "Palette Deck" title');
});

test('suggested palettes panel starts at row height and scrolls only at viewport cap', () => {
  const creationShellCss = STYLE_CSS.match(/\.creation-shell\s*{(?<body>[\s\S]*?)}/).groups.body;
  const creationLayoutCss = STYLE_CSS.match(/\.creation-layout\s*{(?<body>[\s\S]*?)}/).groups.body;
  const creationDeckCss = STYLE_CSS.match(/\.creation-deck\s*{(?<body>[\s\S]*?)}/).groups.body;
  const deckCardsCss = STYLE_CSS.match(/\.creation-deck \.deck-cards\s*{(?<body>[\s\S]*?)}/).groups.body;

  assert.ok(!creationShellCss.includes('height: 100%'), 'creation shell should not force the palette row to viewport height');
  assert.ok(!creationLayoutCss.includes('flex: 1'), 'creation row should size from its content instead of filling the tab');
  assert.ok(creationLayoutCss.includes('align-items: flex-start'), 'side panel should be free to grow taller than the left panel');
  assert.ok(APP_JS.includes('function syncCreationSidePanelSizing()'), 'side panel should sync its starting height from the active left panel');
  assert.ok(APP_JS.includes('sidePanel.style.minHeight = `${sourceHeight}px`;'), 'side panel minimum height should match the measured left panel');
  assert.ok(APP_JS.includes('window.addEventListener("resize", syncCreationSidePanelSizing);'), 'side panel height should resync on viewport resize');
  assert.ok(/max-height:\s*calc\(100vh - 48px\);/.test(creationDeckCss), 'side panel should grow close to the viewport bottom before scrolling');
  assert.ok(/overflow-y:\s*auto;/.test(deckCardsCss), 'suggested palette cards should scroll once the side panel reaches its cap');
  assert.ok(/min-height:\s*0;/.test(deckCardsCss), 'scrollable card list should be allowed to shrink inside the capped panel');
});

test('suggested palette cards use compact vertical spacing', () => {
  const deckCardCss = STYLE_CSS.match(/\.deck-card\s*{(?<body>[\s\S]*?)}/).groups.body;
  const deckHeaderCss = STYLE_CSS.match(/\.deck-card-header\s*{(?<body>[\s\S]*?)}/).groups.body;
  const deckTitleCss = STYLE_CSS.match(/\.deck-card-title\s*{(?<body>[\s\S]*?)}/).groups.body;
  const deckChipsCss = STYLE_CSS.match(/\.deck-card-chips\s*{(?<body>[\s\S]*?)}/).groups.body;
  const deckGamutCss = STYLE_CSS.match(/\.deck-card-gamut\s*{(?<body>[\s\S]*?)}/).groups.body;

  assert.ok(/padding:\s*3px 6px;/.test(deckCardCss), 'suggestion cards should keep tight vertical padding');
  assert.ok(/margin-bottom:\s*1px;/.test(deckHeaderCss), 'suggestion card header should sit close to chip row');
  assert.ok(/line-height:\s*1\.15;/.test(deckTitleCss), 'suggestion title should avoid excess line box height');
  assert.ok(/margin-bottom:\s*1px;/.test(deckChipsCss), 'suggestion chip row should sit close to metric row');
  assert.ok(/line-height:\s*1\.15;/.test(deckGamutCss), 'suggestion metric row should avoid excess line box height');
});
