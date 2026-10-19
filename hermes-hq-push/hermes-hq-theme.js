// Hermes HQ theme: a desktop Hermes theme that looks like Hermes HQ on an iPhone (and Nous High Contrast, below).
//
// It adds the theme "Hermes HQ" to Settings › Appearance (and Cmd-K, /skin), through desktop's own `themes`
// contribution area: no Hermes code changes. The palette is the phone's iOS one (src/ui-kit/ios-theme.ts and
// src/phone-ios-tokens.css): white page and #f2f2f7 sidebar in light, true black and #1c1c1e in dark, iOS system
// blue, the SF system font. The CSS below gives it the phone's shapes: iOS corner radii instead of desktop's nearly
// square ones, your messages as filled blue bubbles on the right, a bot's text in grey bubbles on the left (its tool
// rows outside them), a round composer and rounded sidebar rows. Beyond the chat it restyles desktop's shared
// parts, so every page follows: capsule buttons and tint-coloured text buttons, iOS's green switches, round check
// boxes, grey-filled fields, segmented tabs, frosted round menus, solid round sheets over a light dim, Settings as a
// grouped list of white cards, round faces in the sidebar, and sentence case in place of spaced-out capitals.
//
// The CSS keys on desktop's data-slot hooks and a few of its class names, so a Hermes update can move them; the
// palette part keeps working either way. The renderer sets the palette's own tokens inline on <html>, so the CSS
// paints the bubbles from its own --hq-* variables rather than overriding those.
// Installed as ~/.hermes/desktop-plugins/hermes-hq-theme/plugin.js; desktop reloads it when the file changes.

const SYSTEM_SANS = '-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif, "Apple Color Emoji"'
const SYSTEM_MONO = 'ui-monospace, "SF Mono", Menlo, monospace, "Apple Color Emoji"'

// HQ's standard look (HermesKit/Theme/ThemeTokens.swift, ThemeResolver.base): the Nous ink and page, HQ's blue,
// iOS's separators and fills. Not iOS's own white/black and #007aff: that is the separate "iOS System" skin.
export const LIGHT = {
  background: '#fefefe', foreground: '#1f2328', card: '#f2f2f7', cardForeground: '#1f2328',
  muted: '#f6f8fa', mutedForeground: '#595c60', popover: '#ffffff', popoverForeground: '#1f2328',
  primary: '#0053fd', primaryForeground: '#ffffff', secondary: '#e6eeff', secondaryForeground: '#1f2328',
  accent: '#e6eeff', accentForeground: '#1f2328', border: '#c6c6c8', input: '#fefefe', ring: '#0053fd',
  midground: '#0053fd', midgroundForeground: '#ffffff', composerRing: '#0053fd',
  destructive: '#d70015', destructiveForeground: '#ffffff',
  sidebarBackground: '#f6f8fa', sidebarBorder: '#c6c6c8',
  userBubble: '#0053fd', userBubbleBorder: '#0053fd',
}

export const DARK = {
  background: '#0d1015', foreground: '#e6edf3', card: '#1c1c1e', cardForeground: '#e6edf3',
  muted: '#1c1c1e', mutedForeground: '#aeb4b9', popover: '#1c1c1e', popoverForeground: '#e6edf3',
  primary: '#4a84fe', primaryForeground: '#ffffff', secondary: '#15254a', secondaryForeground: '#e6edf3',
  accent: '#15254a', accentForeground: '#e6edf3', border: '#38393d', input: '#0d1015', ring: '#4a84fe',
  midground: '#4a84fe', midgroundForeground: '#ffffff', composerRing: '#4a84fe',
  destructive: '#ff453a', destructiveForeground: '#ffffff',
  sidebarBackground: '#010409', sidebarBorder: '#38393d',
  userBubble: '#3863bf', userBubbleBorder: '#3863bf',
}

const USER = "[data-slot='aui_user-message-root'] .composer-human-message:not(.ui-prompt-input__container)"
const BOT = "[data-slot='aui_assistant-message-content'] .aui-md:not(.aui-md .aui-md, details .aui-md)"
// A group chat's room: the column that holds its log and, under a top rule, its message form.
const ROOM = ".flex-col:has(> .border-t > form.grid textarea[data-slot='textarea'])"

export const CSS = `
/* iOS shapes: desktop pins its Tailwind corners at 0.2 of their size; the phone uses them whole. */
html:root {--radius-scalar:1}
/* Chat text at the phone's reading size, a step under iOS's 17 pt body for a desktop window. */
html:root {--conversation-text-font-size:15px}

/* Your messages: a filled bubble on the right, as wide as its text, white type. Painted here, not through the
   palette's userBubble, which desktop mixes toward the page (and the Message Bubble transparency setting). */
html:root {--hq-bubble-me:#0053fd}
html:root.dark {--hq-bubble-me:#3863bf}
${USER} {
  position:relative;width:fit-content;max-width:min(80%,36rem);margin-left:auto;
  padding:8px 13px;border:0;border-radius:12px 12px 0 12px;
  background:var(--hq-bubble-me);color:#fff;box-shadow:none;backdrop-filter:none}
${USER} :is(*,a,.ref) {color:inherit}
${USER} a {text-decoration:underline;text-underline-offset:.15em}
[data-slot='aui_user-message-root'] .composer-human-message-container {background:transparent;justify-content:flex-end}

/* A bot's text: a grey bubble on the left, as on the phone (--ios-bubble-bot). Its tool rows stay outside, as the
   reply's work trail. Only the reply's own text blocks: markdown nested in a tool's output keeps its plain look. */
html:root {--hq-bubble-bot:#e9e9eb}
html:root.dark {--hq-bubble-bot:#26282c}
${BOT} {
  position:relative;width:fit-content;max-width:min(88%,44rem);box-sizing:border-box;
  padding:8px 13px;border-radius:12px 12px 12px 0;background:var(--hq-bubble-bot)}
/* HQ's Telegram tail: a flare off the bubble's outer bottom corner, curving from its side to a point on the
   baseline. Filled outside an ellipse centred on the tail box's top inner corner. */
:is(${USER},${BOT})::after {content:'';position:absolute;bottom:0;width:8px;height:15px;pointer-events:none}
${USER}::after {right:-8px;background:radial-gradient(ellipse 100% 100% at 100% 0,transparent 98%,var(--hq-bubble-me) 100%)}
${BOT}::after {left:-8px;background:radial-gradient(ellipse 100% 100% at 0 0,transparent 98%,var(--hq-bubble-bot) 100%)}
/* The boxes round a bubble clip it; they clip 8 px out instead, so the tail shows and long text still can't spill. */
:is([data-slot='aui_assistant-message-content'],[data-slot='aui_user-message-root']) .overflow-hidden:has(:is(.aui-md,.composer-human-message)),
[data-slot='aui_assistant-message-content'].overflow-hidden, [data-slot='aui_assistant-message-root'].overflow-hidden,
:is(${USER},${BOT}).overflow-hidden {overflow:clip;overflow-clip-margin:8px}
${USER} {overflow:visible}   /* desktop's overflow-y:auto never scrolls (no max height) */
${BOT} > div > :first-child {margin-top:0}
${BOT} > div > :last-child {margin-bottom:0}

/* The composer: HQ's message field, one grey capsule with no outline (fill 2 laid on the page, ChatPalette). */
body {--hq-field:#e9e9ea}
html:root.dark body {--hq-field:#2f3137}
html:root body :is([data-slot='composer-root'],[data-slot='composer-root'][data-thread-scrolled-up]) {--composer-fill:var(--hq-field)}
html:root body [data-slot='composer-surface'] {border-radius:22px;border-color:transparent !important}   /* desktop's is !important */
[data-slot='composer-rich-input'] {font-size:15px;line-height:20px}

/* Code and cards on the phone's card shape. */
:is([data-slot='code-card'],[data-slot='file-diff-panel'],.aui-md pre) {border-radius:10px}
.aui-md :not(pre) > code {border-radius:6px}
/* Chat cards (changed files, questions, artifacts) on HQ's card grey; diffs in iOS green and red. */
html:root body {--ui-widget-surface-background:var(--hq-card);
  --ui-diff-add-border:#34c759;--ui-diff-add-background:rgb(52 199 89/.12);--ui-diff-add-foreground:#248a3d;
  --ui-diff-remove-border:#ff3b30;--ui-diff-remove-background:rgb(255 59 48/.1);--ui-diff-remove-foreground:#d70015}
html:root.dark body {--ui-diff-add-border:#30d158;--ui-diff-add-background:rgb(48 209 88/.16);--ui-diff-add-foreground:#30db5b;
  --ui-diff-remove-border:#ff453a;--ui-diff-remove-background:rgb(255 69 58/.16);--ui-diff-remove-foreground:#ff6961}
.rounded-3xl[class*='bg-(--ui-widget-surface-background)'] {border-radius:var(--hq-card-r)}

/* ---- The rest of the app: iOS's colours and shapes on desktop's shared parts. ---- */

/* HQ's tokens (ThemeTokens.swift base, iOS's fills and separators), on <body> so they sit under the palette desktop
   sets on <html>. */
body {
  --hq-cell:#fff;--hq-grouped:#f2f2f7;--hq-raised:#fff;--hq-card:#f2f2f7;--hq-card-r:20px;
  --hq-page:#fefefe;--hq-sidebar:#f6f8fa;--hq-ink:#1f2328;--hq-tint-text:#0040dd;
  --hq-sep:rgb(60 60 67/.29);--hq-label-2:rgb(31 35 40/.74);--hq-fill-4:rgb(116 116 128/.08);
  --hq-fill:rgb(120 120 128/.2);--hq-fill-2:rgb(120 120 128/.16);--hq-fill-3:rgb(118 118 128/.12);
  --hq-green:#34c759;--hq-thumb-shadow:0 3px 8px rgb(0 0 0/.15),0 3px 1px rgb(0 0 0/.06);
  --hq-menu-shadow:0 12px 40px rgb(0 0 0/.16),0 0 0 .5px rgb(0 0 0/.08);
  --hq-tint:var(--theme-primary)}
html:root.dark body {
  --hq-cell:#1c1c1e;--hq-grouped:#000;--hq-raised:#2c2c2e;--hq-card:#1c1c1e;
  --hq-page:#0d1015;--hq-sidebar:#010409;--hq-ink:#e6edf3;--hq-tint-text:#0a84ff;
  --hq-sep:rgb(84 84 88/.6);--hq-label-2:rgb(230 237 243/.74);--hq-fill-4:rgb(118 118 128/.18);
  --hq-fill:rgb(120 120 128/.36);--hq-fill-2:rgb(120 120 128/.32);--hq-fill-3:rgb(118 118 128/.24);
  --hq-green:#30d158;--hq-thumb-shadow:0 3px 8px rgb(0 0 0/.3),0 3px 1px rgb(0 0 0/.1);
  --hq-menu-shadow:0 12px 40px rgb(0 0 0/.5),0 0 0 .5px rgb(255 255 255/.1)}

/* Desktop's surfaces and lines from HQ's tokens: the page and sidebar exactly (desktop pulls the palette's page toward
   a neutral grey), neutral hairlines instead of accent-tinted ones, grey fills for the open and hovered row. */
body {
  --ui-bg-chrome:var(--hq-page);--ui-bg-sidebar:var(--hq-sidebar);
  --ui-stroke-primary:var(--hq-sep);--ui-stroke-secondary:var(--hq-sep);
  --ui-stroke-tertiary:color-mix(in srgb,var(--hq-sep) 60%,transparent);
  --ui-stroke-quaternary:color-mix(in srgb,var(--hq-sep) 40%,transparent);
  --ui-row-active-background:var(--hq-fill-2);--ui-row-hover-background:var(--hq-fill-4);
  --ui-control-hover-background:var(--hq-fill-3);--ui-control-active-background:var(--hq-fill-2)}

/* Sentence case everywhere: iOS never spaces out capitals for a heading or a tab. */
html:root body .uppercase {text-transform:none;letter-spacing:normal}

/* Buttons: capsules, as iOS 26 draws them. A text button is the tint, never underlined; a grey button has tint text. */
[data-slot='button']:not([data-size='inline'],[data-size='micro'],[data-size='grip'],[data-variant='link'],[data-variant='text'],[data-variant='textStrong']) {border-radius:999px}
[data-slot='button'][data-variant='default'] {font-weight:600}
[data-slot='button']:is([data-variant='secondary'],[data-variant='outline']) {
  background:var(--hq-fill-3);color:var(--hq-tint);box-shadow:none}
[data-slot='button']:is([data-variant='secondary'],[data-variant='outline']):hover {background:var(--hq-fill-2);color:var(--hq-tint)}
[data-slot='button']:is([data-variant='link'],[data-variant='text'],[data-variant='textStrong']) {color:var(--hq-tint-text);text-decoration:none}
[data-slot='button']:is([data-variant='link'],[data-variant='text'],[data-variant='textStrong']):hover {color:var(--hq-tint-text);opacity:.7;text-decoration:none}

/* Switches: iOS's green pill with a white thumb that throws a soft shadow. */
[data-slot='switch'] {width:38px;height:23px;padding:2px;border:0;border-radius:999px;box-shadow:none}
[data-slot='switch'][data-state='checked'] {background:var(--hq-green)}
[data-slot='switch'][data-state='unchecked'] {background:var(--hq-fill)}
[data-slot='switch-thumb'] {width:19px;height:19px;background:#fff;box-shadow:var(--hq-thumb-shadow)}
[data-slot='switch-thumb'] {translate:0 0}
[data-slot='switch-thumb'][data-state='checked'] {translate:15px 0}
[data-slot='switch'].w-7 {width:30px;height:18px}
[data-slot='switch'].w-7 [data-slot='switch-thumb'] {width:14px;height:14px}
[data-slot='switch'].w-7 [data-slot='switch-thumb'][data-state='checked'] {translate:12px 0}

/* Check boxes: round, filled with the tint when on. */
[data-slot='checkbox'] {border-radius:999px}
[data-slot='checkbox'][data-state='checked'] {background:var(--hq-tint);border-color:var(--hq-tint);color:#fff}

/* Fields and pickers: iOS's grey fill, no outline, round corners; the tint rings the one being typed in. */
:is([data-slot='input'],[data-slot='textarea'],[data-slot='select-trigger'],.desktop-input-chrome):not([data-slot='composer-rich-input']) {
  border-radius:10px;border-color:transparent;background:var(--hq-fill-3);box-shadow:none}
:is([data-slot='input'],[data-slot='textarea'],[data-slot='select-trigger'],.desktop-input-chrome):not([data-slot='composer-rich-input']):hover {border-color:transparent}
:is([data-slot='input'],[data-slot='textarea'],[data-slot='select-trigger'],.desktop-input-chrome):not([data-slot='composer-rich-input']):is(:focus,:focus-within,[data-state='open']) {
  border-color:var(--hq-tint);box-shadow:0 0 0 3px color-mix(in srgb,var(--hq-tint) 22%,transparent)}
[data-slot='input-group'] {border-radius:10px}

/* Segmented controls (tabs): a grey track with the chosen segment raised in white. */
[data-slot='tabs-list'] {background:var(--hq-fill-3);border:0;border-radius:9px;padding:2px;gap:0}
[data-slot='tabs-trigger'] {border-radius:7px;border:0;box-shadow:none}
[data-slot='tabs-trigger'][data-state='active'] {background:var(--hq-raised);box-shadow:var(--hq-thumb-shadow)}

/* Menus: iOS's menu shape, round corners and rounded highlights, on a solid surface (no CSS-faked frost). */
:is([data-slot='dropdown-menu-content'],[data-slot='dropdown-menu-sub-content'],[data-slot='context-menu-content'],[data-slot='context-menu-sub-content'],[data-slot='select-content'],[data-slot='command']) {
  border-radius:14px;border-color:transparent;background:var(--hq-raised);box-shadow:var(--hq-menu-shadow)}
:is([data-slot='dropdown-menu-content'],[data-slot='context-menu-content']) {min-width:200px}
[data-slot='command'] {border-radius:16px}
:has(> [data-slot='command']) {border-color:transparent;border-radius:16px;background:var(--hq-raised)}
[data-slot='command'] [cmdk-group-heading] {padding:8px 10px 4px;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--hq-label-2);background:var(--hq-raised)}
[data-slot='command-item'] {min-height:34px;font-size:14px}
[data-slot='command-item'][data-selected='true'] {background:var(--hq-fill-2);color:var(--foreground)}
:is([data-slot='dropdown-menu-item'],[data-slot='dropdown-menu-checkbox-item'],[data-slot='dropdown-menu-radio-item'],[data-slot='dropdown-menu-sub-trigger'],[data-slot='context-menu-item'],[data-slot='context-menu-checkbox-item'],[data-slot='context-menu-sub-trigger'],[data-slot='select-item'],[data-slot='command-item']) {border-radius:8px}
:is([data-slot='dropdown-menu-separator'],[data-slot='context-menu-separator'],[data-slot='select-separator'],[data-slot='command-separator']) {background:var(--hq-sep)}
[data-slot='popover-content'] {border-radius:14px;border-color:transparent;background:var(--hq-raised);box-shadow:var(--hq-menu-shadow)}
[data-slot='popover-content'] > [data-slot='command'] {border-radius:10px;background:transparent;box-shadow:none}
[data-slot='tooltip-content'] {border-radius:8px}
/* Toasts: HQ's banner, a solid raised card with round corners and the menu shadow, no outline and no CSS frost. */
[role='region'] > [data-slot='card-stack'] :is([data-slot='card-stack-surface'],[data-slot='card-stack-edge']) {
  border:0;border-radius:16px;background:var(--hq-raised);box-shadow:var(--hq-menu-shadow);-webkit-backdrop-filter:none;backdrop-filter:none}
[role='region'] > [data-slot='card-stack'] [data-slot='alert'] {padding-block:12px;font-size:13px}
[role='region'] > [data-slot='card-stack'] [data-slot='alert-title'] {font-size:14px;font-weight:600;letter-spacing:-.01em}
[role='region'] > [data-slot='card-stack'] [data-slot='alert-description'] {font-size:13px;line-height:18px;color:var(--hq-label-2)}
/* Tool approvals: an HQ card in the thread, raised with a shadow when it floats over the chat. */
[data-slot='tool-approval-stack'] [data-slot='card-stack'] :is([data-slot='card-stack-surface'],[data-slot='card-stack-edge']) {border:0;border-radius:var(--hq-card-r);background:var(--hq-card);box-shadow:none}
[data-slot='tool-approval-stack'][data-approval-placement='floating'] [data-slot='card-stack'] :is([data-slot='card-stack-surface'],[data-slot='card-stack-edge']) {background:var(--hq-raised);box-shadow:var(--hq-menu-shadow)}
[data-slot='tool-approval-card'] > div:first-child {padding:12px 16px 0;font-size:13px;color:var(--hq-label-2)}
[data-slot='tool-approval-card'] > pre {padding:8px 16px;font-size:13px;line-height:1.45}
[data-slot='tool-approval-actions'] {gap:8px;padding:4px 12px 12px}
[data-slot='tool-approval-actions'] [data-slot='button'][data-size='sm'] {padding:6px 14px;font-size:13px;line-height:18px}
[data-approval-activity][data-glass-opaque] {border-radius:10px}
:is([data-slot='badge'],[data-slot='kbd']) {border-radius:6px}

/* Dialogs and the big pages that open over the chat (Settings, Jobs, Profiles…): a light dim, no blur, and a
   card with the phone's sheet corners. */
:is([data-slot='dialog-overlay'],[data-slot='sheet-overlay'],[data-overlay-surface]) {background-color:rgb(0 0 0/.2);backdrop-filter:none}
html:root.dark :is([data-slot='dialog-overlay'],[data-slot='sheet-overlay'],[data-overlay-surface]) {background-color:rgb(0 0 0/.48)}
[data-slot='dialog-content'] {border-radius:22px;border-color:transparent;background:var(--hq-raised);box-shadow:0 20px 60px rgb(0 0 0/.22)}
[data-overlay-surface] [data-glass-raised] {border-radius:22px;border-color:transparent;background-color:var(--hq-grouped);box-shadow:0 20px 60px rgb(0 0 0/.22)}
/* A sheet is iOS's elevated grouped list: grey page and white cards; in dark the page lifts to #1c1c1e and the cards to
   #2c2c2e, so the sheet stands off the dimmed window. */
[data-overlay-surface] {--hq-grouped:#f2f2f7;--hq-cell:#fff}
html:root.dark [data-overlay-surface] {--hq-grouped:#1c1c1e;--hq-cell:#2c2c2e}

/* Settings: iOS's grouped list. Grey page, each group of options a white card, hairlines between rows. */
[data-overlay-surface] [data-glass-raised]:has([id^='setting-field-']) :is(main,div:has(> main)) {background:var(--hq-grouped)}
:is(:has(> [id^='setting-field-']):not(.overflow-y-auto),[data-overlay-surface] .grid.gap-1:has(> :is(.scroll-mt-6.rounded-lg,.\\@container) .grid.gap-3.py-3)) {
  gap:0;padding:0 16px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
/* Loose rows (ListRow, or a field straight in the page) run together into one grouped card. */
[data-overlay-surface] main :is(.overflow-y-auto,:not(:has(> [id^='setting-field-'])):not(.grid.gap-1,[id^='setting-field-'],.scroll-mt-6,.rounded-xl,.border-b,[class*='bg-('])) > :is([id^='setting-field-'],.\\@container:has(> .grid.gap-3.py-3)) {padding:0 16px;background:var(--hq-cell)}
[data-overlay-surface] main :is(.overflow-y-auto,:not(:has(> [id^='setting-field-'])):not(.grid.gap-1,[id^='setting-field-'],.scroll-mt-6,.rounded-xl,.border-b,[class*='bg-('])) > :not(:is([id^='setting-field-'],.\\@container:has(> .grid.gap-3.py-3))) + :is([id^='setting-field-'],.\\@container:has(> .grid.gap-3.py-3)), [data-overlay-surface] main :is(.overflow-y-auto,:not(:has(> [id^='setting-field-'])):not(.grid.gap-1,[id^='setting-field-'],.scroll-mt-6,.rounded-xl,.border-b,[class*='bg-('])) > :is([id^='setting-field-'],.\\@container:has(> .grid.gap-3.py-3)):first-child {border-top-left-radius:var(--hq-card-r);border-top-right-radius:var(--hq-card-r)}
[data-overlay-surface] main :is(.overflow-y-auto,:not(:has(> [id^='setting-field-'])):not(.grid.gap-1,[id^='setting-field-'],.scroll-mt-6,.rounded-xl,.border-b,[class*='bg-('])) > :is([id^='setting-field-'],.\\@container:has(> .grid.gap-3.py-3)):not(:has(+ [id^='setting-field-'], + .\\@container > .grid.gap-3.py-3)) {margin-bottom:20px;border-bottom-left-radius:var(--hq-card-r);border-bottom-right-radius:var(--hq-card-r)}
[data-overlay-surface] main :is(.overflow-y-auto,:not(:has(> [id^='setting-field-'])):not(.grid.gap-1,[id^='setting-field-'],.scroll-mt-6,.rounded-xl,.border-b,[class*='bg-('])) > :is([id^='setting-field-'],.\\@container:has(> .grid.gap-3.py-3)) + :is([id^='setting-field-'],.\\@container:has(> .grid.gap-3.py-3)) {box-shadow:0 -.5px 0 var(--hq-sep)}
/* A field that opens with its own intro (Pet): the intro reads as the group's header, the rest is the card. */
[data-overlay-surface] main [id^='setting-field-']:has(> div > p.max-w-2xl:first-child + .mt-2) {padding:0!important;background:none!important;box-shadow:none!important}
[data-overlay-surface] main [id^='setting-field-'] > div > p.max-w-2xl:first-child:has(+ .mt-2) {padding:0 16px 6px;font-size:13px;color:var(--hq-label-2)}
[data-overlay-surface] main [id^='setting-field-'] > div > p.max-w-2xl:first-child + .mt-2 {margin-top:0;padding:0 16px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
/* Billing: the balance strip and the usage meters are cards too. */
[data-overlay-surface] main .\\@container.mb-6:has(> .grid > .min-w-0 > .text-lg.font-semibold) {padding:12px 16px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main section > .\\@container:has(> .\\@container > .grid.gap-2.py-3) {padding:0 16px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main section > .\\@container > .\\@container + .\\@container {box-shadow:0 -.5px 0 var(--hq-sep)}
/* About: the update checks are one card of plain rows, the build facts a second card; no boxes inside boxes. */
[data-overlay-surface] main .mx-auto.max-w-2xl:has(> [id^='setting-field-about.']) {padding:0!important;background:none!important}
[data-overlay-surface] main .mx-auto.max-w-2xl > .mb-2\\.5:first-child {padding:0 16px;font-size:13px;font-weight:400;color:var(--hq-label-2)}
[data-overlay-surface] main [id^='setting-field-about.'].grid {gap:0;padding:0 16px;border-radius:var(--hq-card-r)!important;background:var(--hq-cell)}
[id^='setting-field-about.'] > .rounded-xl.border {padding:12px 0;border:0;border-radius:0;background:none}
[id^='setting-field-about.'] > .rounded-xl.border .mt-3 {margin-top:6px}
[id^='setting-field-about.'] > * + * {box-shadow:0 -.5px 0 var(--hq-sep)}
[data-overlay-surface] main .mx-auto.max-w-2xl > dl.rounded-lg.border {gap:0;margin-top:20px;padding:4px 16px;border:0;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main .mx-auto.max-w-2xl > dl.rounded-lg.border > div {padding:9px 0}
[data-overlay-surface] main .mx-auto.max-w-2xl > dl.rounded-lg.border > div + div {box-shadow:0 -.5px 0 var(--hq-sep)}
/* Keys (Providers › API keys, Tools & Keys): the list is one card; an opened key stays in its row, no ring. */
[data-overlay-surface] main .grid.gap-2:has(> .scroll-mt-6.rounded-\\[6px\\]) {gap:0;padding:0 16px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main .scroll-mt-6.rounded-\\[6px\\] > .group\\/card {padding:10px 0;border-radius:0;background:none!important;--tw-ring-shadow:0 0 #0000;box-shadow:none}
[data-overlay-surface] main .scroll-mt-6.rounded-\\[6px\\] + .scroll-mt-6.rounded-\\[6px\\] {box-shadow:0 -.5px 0 var(--hq-sep)}
[data-overlay-surface] main button.group.grid.rounded-\\[6px\\]:has(> .flex-col > .font-semibold) {margin-bottom:20px;padding:12px 16px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
/* Providers › Accounts: each run of account rows is one card, iOS-inset hairlines. */
[data-overlay-surface] main section.grid.gap-2:has(> :is(button.group.flex,div.group.grid).rounded-\\[6px\\]) {gap:0}
[data-overlay-surface] main section.grid.gap-2:has(> :is(button.group.flex,div.group.grid).rounded-\\[6px\\]) > p {margin:20px 0 6px;padding:0 16px;font-size:13px;font-weight:400;color:var(--hq-label-2)}
[data-overlay-surface] main section.grid.gap-2 > :is(button.group.flex,div.group.grid).rounded-\\[6px\\] {border-radius:0;background-color:var(--hq-cell)}
[data-overlay-surface] main section.grid.gap-2 > :is(button.group.flex,div.group.grid).rounded-\\[6px\\] + :is(button.group.flex,div.group.grid).rounded-\\[6px\\] {background-image:linear-gradient(var(--hq-sep),var(--hq-sep));background-size:calc(100% - 32px) .5px;background-position:16px 0;background-repeat:no-repeat}
[data-overlay-surface] main :not(:is(button.group.flex,div.group.grid).rounded-\\[6px\\]) + section.grid.gap-2 > :is(button.group.flex,div.group.grid).rounded-\\[6px\\] {border-top-left-radius:var(--hq-card-r);border-top-right-radius:var(--hq-card-r)}
[data-overlay-surface] main section.grid.gap-2 > :is(button.group.flex,div.group.grid).rounded-\\[6px\\]:not(:has(+ :is(button.group.flex,div.group.grid).rounded-\\[6px\\])) {border-bottom-left-radius:var(--hq-card-r);border-bottom-right-radius:var(--hq-card-r)}
[data-overlay-surface] main section.grid.gap-2 > :is(button.group.flex,div.group.grid).rounded-\\[6px\\] :is(button.min-w-0,&) {padding-inline:16px}
[data-overlay-surface] main section.grid.gap-2 > :is(button.group.flex,div.group.grid).rounded-\\[6px\\]:is(div) {padding-inline:0}
/* Keyboard Shortcuts: categories are footnote headers over a card of bindings. */
[data-overlay-surface] main section:has(> button.group\\/kbd-cat) {margin-bottom:20px}
[data-overlay-surface] main .px-2.py-1\\.5:has(> section > button.group\\/kbd-cat) {padding:0}
[data-overlay-surface] main button.group\\/kbd-cat {padding:0 16px 6px}
[data-overlay-surface] main button.group\\/kbd-cat > span {font-size:13px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--hq-label-2)}
[data-overlay-surface] main section:has(> button.group\\/kbd-cat) > .flex.rounded-lg {padding:8px 16px;border-radius:0;background-color:var(--hq-cell)}
[data-overlay-surface] main section:has(> button.group\\/kbd-cat) > .flex.rounded-lg + .flex.rounded-lg {background-image:linear-gradient(var(--hq-sep),var(--hq-sep));background-size:calc(100% - 32px) .5px;background-position:16px 0;background-repeat:no-repeat}
[data-overlay-surface] main section:has(> button.group\\/kbd-cat) > .flex.rounded-lg:first-of-type {border-top-left-radius:var(--hq-card-r);border-top-right-radius:var(--hq-card-r)}
[data-overlay-surface] main section:has(> button.group\\/kbd-cat) > .flex.rounded-lg:last-child {border-bottom-left-radius:var(--hq-card-r);border-bottom-right-radius:var(--hq-card-r)}
[data-overlay-surface] main section:has(> button.group\\/kbd-cat) > .flex.rounded-lg > span {font-size:14px}
/* Two loose rows in a spaced grid still read as one card (Gateways › Remote URL + Authentication). */
[data-overlay-surface] main .grid:has(> .\\@container + .\\@container > .grid.gap-3.py-3):not(.grid.gap-1) {row-gap:0}
/* Custom endpoints: the saved list and the editor are cards, not outlined boxes. */
[data-overlay-surface] main section > :is(.divide-y,.grid).rounded-md.border {border:0;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main section > .grid.rounded-md.border.p-3 {padding:16px}
[data-overlay-surface] main section > .grid.rounded-md.border :is([data-slot='input'],[data-slot='select-trigger'],[data-slot='textarea'],.desktop-input-chrome) {background:var(--hq-fill-3)}
/* Uninstall: a plain card; each choice is a row, hairline-separated, nothing outlined in red. */
[data-overlay-surface] main .mx-auto.max-w-2xl > .rounded-xl.border {padding:12px 16px;border:0;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main .mx-auto.max-w-2xl > .rounded-xl.border .mt-1.flex-col.gap-2 {gap:0;margin-top:6px}
[data-overlay-surface] main .mx-auto.max-w-2xl > .rounded-xl.border button.items-start.rounded-lg.border {padding:11px 0;border:0;border-radius:0;background:none}
[data-overlay-surface] main .mx-auto.max-w-2xl > .rounded-xl.border button.items-start.rounded-lg.border + button {box-shadow:0 -.5px 0 var(--hq-sep)}
[id^='setting-field-']:not(.overflow-y-auto > *), .grid.gap-1 > .scroll-mt-6.rounded-lg {border-radius:0}
[data-overlay-surface] .grid.gap-1:has(> :is(.scroll-mt-6.rounded-lg,.\\@container) .grid.gap-3.py-3) + * {margin-top:20px}
:is([id^='setting-field-'],.grid.gap-1 > .scroll-mt-6.rounded-lg,.grid.gap-1 > .\\@container) + :is([id^='setting-field-'],.scroll-mt-6.rounded-lg,.\\@container) {box-shadow:0 -.5px 0 var(--hq-sep)}

/* The Sessions list: section headings as the Bots tab's (quiet, sentence case, no dithered tint square); date groups
   a step quieter; titles at HQ's list size. */
button.group\\/section-label > span.tracking-\\[0\\.16em\\] {padding-left:4px;font-size:13px;line-height:16px;font-weight:600;letter-spacing:0;color:var(--hq-label-2)}
.dither.size-2 {display:none}
.group\\/workspace .tracking-\\[0\\.12em\\] {font-size:12px;font-weight:500;letter-spacing:0;color:var(--hq-label-2);opacity:.8}
.row-hover .hover-marquee.text-\\[0\\.8125rem\\] {font-size:14px;color:var(--foreground)}

/* ---- The Bots list: HQ's Bots tab. A large title, quiet sentence-case section headings with the count after a dot
   and the chevron at the end, rows of round faces with the name over a two-line preview, hairlines inset to the
   text, and the one signal that matters (unread, working, needs you) as a dot at the row's end. ---- */
:has(> [data-slot='bots-roster']) > .justify-between:first-child {padding:14px 12px 4px 16px}
:has(> [data-slot='bots-roster']) > .justify-between:first-child > span {
  font-size:22px;line-height:28px;font-weight:700;letter-spacing:-.02em;color:var(--foreground);text-transform:none}
[data-slot='sidebar-wrapper'] .border-b:has(> input[placeholder^='Search']) {
  display:flex;width:100%;height:34px;padding:0 10px;border:0;border-radius:10px;background:var(--hq-fill-3);opacity:1}
[data-slot='sidebar-wrapper'] .border-b:has(> input[placeholder^='Search']) > input {font-size:14px;field-sizing:fixed}

[data-slot='bots-roster'] {--ui-row-active-background:var(--hq-fill-2)}
[data-slot='bots-roster'] .grid:has(> [data-roster-key]) {gap:0}
[data-slot='bots-section'] {margin-left:0;padding-left:0;border-left:0}

[data-slot='bots-roster'] button.tracking-wider {
  gap:5px;margin-top:12px;padding:5px 8px 5px 10px;font-size:13px;font-weight:600;letter-spacing:0;text-transform:none;
  color:var(--hq-label-2)}
[data-slot='bots-roster'] button.tracking-wider > .codicon:not(.codicon-chevron-right) {display:none}
[data-slot='bots-roster'] button.tracking-wider > .codicon-chevron-right {order:3;font-size:11px !important;opacity:.6}
[data-slot='bots-roster'] button.tracking-wider > .tabular-nums {order:1;color:inherit}
[data-slot='bots-roster'] button.tracking-wider > .tabular-nums::before {content:'· '}
[data-slot='bots-roster'] button.tracking-wider > .flex-1 {order:2}

[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] {position:relative;gap:12px;min-height:66px;padding:9px 10px;border-radius:12px}
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] img {width:44px !important;height:44px !important;border-radius:50% !important}   /* sized inline */
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] > .shrink-0:has(> svg), [data-slot='bots-roster'] button[data-slot='context-menu-trigger'] > .shrink-0 > svg {width:44px;height:44px}   /* drawn faces */
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] :is(.w-\\[34px\\]) {width:44px}
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] .size-8 {width:44px;height:44px;border-radius:50%}
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] .font-medium {font-size:15px;line-height:20px;font-weight:600;letter-spacing:-.01em}
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] .items-baseline > .shrink-0 {font-size:12px;color:var(--hq-label-2)}
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] .codicon-pinned {display:none}
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] .text-xs {margin-top:1px;font-size:13px;line-height:17px;color:var(--hq-label-2)}
[data-slot='bots-roster'] button[data-slot='context-menu-trigger'] .text-xs > .truncate {white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
[data-slot='bots-roster'] [data-roster-key] span.size-3\\.5 {display:none}
[data-slot='bots-roster'] [data-roster-key] span.size-3\\.5:has([role='status']) {display:grid;position:absolute;right:12px;bottom:17px}
[data-slot='bots-roster'] [data-roster-key]:has([role='status']) .text-xs {padding-right:18px}
[data-slot='bots-roster'] [data-roster-key] [role='status'] {width:10px;height:10px}
[data-slot='bots-roster'] [data-roster-key] [role='status'][class~='bg-(--ui-success)'] {background:var(--hq-tint)}

[data-slot='bots-roster'] [data-roster-key] + [data-roster-key]::before {
  content:'';position:absolute;top:0;left:66px;right:10px;border-top:1px solid var(--hq-sep);pointer-events:none}
/* A group's move buttons wait for the pointer. */
[data-slot='bots-roster'] .grid:has(> button[data-slot='context-menu-trigger']) > .flex-col {opacity:0;transition:opacity .15s}
[data-slot='bots-roster'] .grid:has(> button[data-slot='context-menu-trigger']):hover > .flex-col {opacity:1}
[data-slot='bots-roster'] :is([data-roster-key][class~='bg-(--ui-row-active-background)'],[data-roster-key]:hover)::before,
[data-slot='bots-roster'] :is([data-roster-key][class~='bg-(--ui-row-active-background)'],[data-roster-key]:hover) + [data-roster-key]::before {border-color:transparent}

/* Tabs (Sessions and Bots, the chats along the top): the open one a grey capsule, as a segmented control, instead
   of an underline; no rules between them. A closeable tab carries the context menu's data-slot, hence data-tree-tab. */
:is([data-slot='pane-tab'],[role='tab'][data-tree-tab]) {font-size:12px;border-left-color:transparent}
:is([data-slot='pane-tab'],[role='tab'][data-tree-tab]):not([data-vertical]) {height:22px;margin:auto 1px;border-radius:999px}
:is([data-slot='pane-tab'],[role='tab'][data-tree-tab])[data-active='true'] {--tab-bg:var(--hq-fill-3);box-shadow:none}
:is([data-slot='pane-tab'],[role='tab'][data-tree-tab])[data-vertical][data-active='true'] {border-radius:999px}

/* ---- Sizes and spacing: HQ's own measures (BotRowMetrics, ChatPalette, the iOS list), a step down for a window
   seen from further away than a phone. ---- */

/* The conversation in one centred column, as on the phone and the iPad, instead of across the whole window: your
   bubbles and the replies stay near each other. Desktop sizes the thread and the composer by this one value. */
body {--composer-width:48rem}

/* Chat: HQ's 6 pt between turns and tight runs of steps; reply text at HQ's 1.3 leading, not desktop's 1.5. */
body {--conversation-turn-gap:6px;--turn-block-gap:8px;--tool-row-gap:4px}
:is(${USER},${BOT}) {line-height:1.35}
:is(${USER},${BOT}) :is(p,li) {line-height:inherit}
/* A reply's time and buttons show on hover; they no longer hold a 32 px blank line under every reply, so the next
   turn sits HQ's distance below. */
:has(> * > [data-slot='aui_msg-actions']) {min-height:0;height:18px}
[data-slot='aui_msg-actions'] {padding-block:0}

/* Native glass (Settings › Appearance › Window Translucency › Glass, area Sidebar): macOS's own material behind the
   sidebar, the chat solid, as Finder and HQ's iPad sidebar. Desktop splits the window at its classic sidebar, which
   the pane layout doesn't have (the Bots list never counts), so there it painted the whole window solid. Here the
   page carries the Tint setting's see-through mix and every pane but the sidebar's goes solid. On the glass, rows
   are picked out in iOS's grey fill rather than desktop's blue tint. */
:root[data-hermes-glass][data-hermes-glass-scope='sidebar'] body:has([data-tree-group]) {
  background:color-mix(in srgb,var(--ui-bg-chrome) var(--translucency-glass-keep,100%),transparent)}
:root[data-hermes-glass][data-hermes-glass-scope='sidebar'] [data-tree-group]:not(:has([data-slot='bots-roster'],[data-slot='sidebar'])) {
  background-color:var(--ui-bg-chrome)}
:root[data-hermes-glass][data-hermes-glass-scope='sidebar'] :is([data-slot='bots-roster'],[data-slot='sidebar']) {
  --ui-row-active-background:var(--hq-fill-2);--chrome-action-hover:var(--hq-fill-3)}

/* Tabs a size up, with room round the label. */
:is([data-slot='pane-tab'],[role='tab'][data-tree-tab]) {font-size:13px}
:is([data-slot='pane-tab'],[role='tab'][data-tree-tab]):not([data-vertical]) {height:24px;padding-inline:4px}

/* A chat's tabs: HQ's title over the conversation. Centred, the open one a semibold capsule that shows the whole
   name, the others plain grey names; the idle dot goes (a working or waiting tab keeps its signal). */
[role='tablist']:has(> .contents > [data-tree-tab='workspace']) > .contents > :first-child {margin-left:auto}
[role='tablist']:has(> .contents > [data-tree-tab='workspace']) > :last-child {margin-right:auto}
[role='tab'][data-tree-tab='workspace'] {max-width:24rem;font-size:13px;color:var(--hq-label-2)}
[role='tab'][data-tree-tab='workspace']:not([data-vertical]) {height:28px;padding-inline:6px}
[role='tab'][data-tree-tab='workspace'][data-active='true'] {font-weight:600;color:var(--foreground)}
[role='tab'][data-tree-tab='workspace'] .tab-key-hint-icon:has(> * > [class~='bg-(--ui-text-quaternary)']) {display:none}

/* Settings: the options in a readable centred column, as iPad Settings, on cards with HQ's round corners; the list
   of sections down the side at iOS's row height. */
[data-overlay-surface] [data-glass-raised]:has([id^='setting-field-']) main > * {width:100%;max-width:54rem;margin-inline:auto}
[id^='setting-field-'] > * > * {min-height:44px}
[data-overlay-surface] aside button:not([data-slot='button']) {height:auto;min-height:34px;font-size:14px;border-radius:10px;border-color:transparent}
/* The section list is iPad Settings' sidebar: solid (under Glass desktop leaves it see-through, showing the dimmed
   window behind the sheet), a hairline from the options, the open page in the tint with white text, the open
   group's name in the headline weight instead of a boxed fill. */
[data-overlay-surface] [data-glass-raised] aside {background:var(--hq-cell);box-shadow:inset -1px 0 0 var(--hq-sep)}
[data-overlay-surface] aside button:not([data-slot='button']):not([aria-current='page']) {background:transparent}
[data-overlay-surface] aside button:not([data-slot='button']):not([aria-current='page']):hover {background:var(--hq-fill-3)}
[data-overlay-surface] aside button[class~='bg-(--ui-bg-tertiary)'] {font-weight:600}
[data-overlay-surface] aside button[aria-current='page'] {background:var(--hq-tint);color:#fff;font-weight:500}
[data-overlay-surface] aside button[aria-current='page'] :is(svg,i) {color:#fff}
/* A field lying on the grey page, outside a card, is white, as iOS draws one; inside a card it keeps the grey fill. */
[data-overlay-surface] main :is([data-slot='input'],[data-slot='select-trigger'],[data-slot='textarea']):not([id^='setting-field-'] *) {background:var(--hq-cell)}

/* Controls at a size a pointer finds easily, iOS's proportions: buttons 30 px, fields 34 px, menu rows 30 px. */
[data-slot='button'][data-size='default'] {padding:6px 14px;font-size:13px;line-height:18px}
[data-slot='button'][data-size='sm'] {padding:4px 12px;font-size:12.5px}
[data-slot='button'][data-size='lg'] {padding:9px 20px;font-size:15px}
:is([data-slot='input'],[data-slot='select-trigger'],[data-slot='searchable-select-trigger'],input.desktop-input-chrome) {min-height:34px;padding-inline:12px;font-size:13px;line-height:18px}
[data-slot='textarea'] {padding:8px 12px;font-size:13px}
/* A value longer than its field ends in an ellipsis inside the fill rather than running past it. */
[data-slot='select-trigger'] > span:first-child {min-width:0;overflow:hidden;text-overflow:ellipsis}
:is([data-slot='dropdown-menu-content'],[data-slot='context-menu-content'],[data-slot='select-content'],[data-slot='dropdown-menu-sub-content'],[data-slot='context-menu-sub-content']) {padding:6px}
:is([data-slot='dropdown-menu-item'],[data-slot='dropdown-menu-checkbox-item'],[data-slot='dropdown-menu-radio-item'],[data-slot='dropdown-menu-sub-trigger'],[data-slot='context-menu-item'],[data-slot='context-menu-checkbox-item'],[data-slot='context-menu-sub-trigger'],[data-slot='select-item'],[data-slot='command-item']) {min-height:30px;font-size:13px}
[data-slot='tabs-trigger'] {height:26px;padding-inline:12px;font-size:13px}

/* Pages over the chat (Jobs, Profiles): their lists at a list's size rather than a file tree's, the page title and
   the selected item's name a step up, as the headline over an iOS list. */
[data-overlay-surface] .group\\/row:has(> [data-slot='row-button']) {height:32px;flex-shrink:0;font-size:13px;border-radius:8px}
@media (min-width:47.5rem) {
  [data-overlay-surface] .shrink-0.flex-col:has(> * > .group\\/row > [data-slot='row-button']) {width:16rem}   /* room for job names */
}
[data-overlay-surface] [data-glass-raised] h2.text-sm.font-semibold {font-size:17px}
[data-overlay-surface] [data-glass-raised] h3.font-semibold.tracking-tight {font-size:17px}

/* Scheduled jobs: a large title, the jobs in an inset card, and the open job's facts, script and runs as grouped
   cards with hairlines between rows and sentence-case footnote headings over them. */
[data-overlay-surface] header > .min-w-0 > h2.text-sm.font-semibold {font-size:22px;line-height:28px;font-weight:700;letter-spacing:-.02em}
[data-overlay-surface] header > .min-w-0 > h2 + * {font-size:13px;color:var(--hq-label-2)}
[data-overlay-surface] .inline-flex.border-b:has(> input) {height:32px;margin-bottom:8px;padding:0 10px;border:0;border-radius:10px;background:var(--hq-cell)}
[data-overlay-surface] .inline-flex.border-b:has(> input) > input {font-size:13px}
[data-overlay-surface] .overflow-y-auto:has(> .group\\/row > [data-slot='row-button']) {padding:4px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] .space-y-4 > header h3 {font-size:20px;line-height:26px;font-weight:700;letter-spacing:-.01em}
[data-overlay-surface] .space-y-4 > header .rounded-full.capitalize {padding:2px 8px;font-size:11px}
[data-overlay-surface] .space-y-4 > header > dl {grid-template-columns:8rem 1fr;gap:0;padding:0 16px;border-radius:var(--hq-card-r);background:var(--hq-cell);font-size:13px}
[data-overlay-surface] .space-y-4 > header > dl :is(dt,dd) {padding:10px 0}
[data-overlay-surface] .space-y-4 > header > dl dt {color:var(--hq-label-2)}
[data-overlay-surface] .space-y-4 > header > dl .contents + .contents > * {box-shadow:inset 0 1px 0 var(--hq-sep)}
[data-overlay-surface] .space-y-4 .text-\\[0\\.6rem\\].tracking-wider {margin-bottom:6px;padding-inline:16px;font-size:13px;color:var(--hq-label-2)}
[data-overlay-surface] .space-y-4 section > pre {padding:12px 16px;border-radius:var(--hq-card-r);background:var(--hq-cell);font-size:12.5px}
[data-overlay-surface] .space-y-4 .tracking-wider + .flex.flex-col.gap-px {gap:0;padding:0 4px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] .space-y-4 .tracking-wider + .flex.flex-col.gap-px > div {position:relative;padding:10px 12px;font-size:13px;border-radius:8px}
[data-overlay-surface] .space-y-4 .tracking-wider + .flex.flex-col.gap-px > div + div::before {
  content:'';position:absolute;top:0;left:12px;right:12px;border-top:1px solid var(--hq-sep)}
[data-overlay-surface] .space-y-4 .tracking-wider + .flex.flex-col.gap-px > div > :last-child {font-size:12px;color:var(--hq-label-2)}

/* Command Center's session list: one inset card, a hairline between sessions from the text's left edge. */
[data-overlay-surface] main ul:has(> li.group.py-2) {margin-block:4px 24px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main ul > li.group.py-2 {position:relative;min-height:54px;padding:8px 12px 8px 16px}
[data-overlay-surface] main ul > li.group.py-2 + li::before {content:'';position:absolute;top:0;left:16px;right:0;border-top:1px solid var(--hq-sep)}
[data-overlay-surface] main ul > li.group.py-2 > button > :first-child {font-size:15px;font-weight:400}
[data-overlay-surface] main ul > li.group.py-2 > button > :last-child {font-size:13px;color:var(--hq-label-2)}

/* Command Center's Maintenance: each section's actions are cells of one card (rounded at the ends, hairlines between),
   under a sentence-case footnote heading. */
[data-overlay-surface] main section > .text-\\[0\\.625rem\\].uppercase {margin-bottom:6px;padding-inline:16px;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--hq-label-2)}
[data-overlay-surface] main section > div:not([class]) > :first-child:not(.py-2) {margin-bottom:6px;padding-inline:16px;font-size:13px;color:var(--hq-label-2)}
[data-overlay-surface] main :is(section,section > div) > .flex.justify-between.gap-3.py-2 {position:relative;min-height:54px;padding:10px 16px;background:var(--hq-cell)}
[data-overlay-surface] main :is(section,section > div) > :not(.py-2) + .flex.justify-between.gap-3.py-2 {border-radius:var(--hq-card-r) var(--hq-card-r) 0 0}
[data-overlay-surface] main :is(section,section > div) > .flex.justify-between.gap-3.py-2:last-child {border-bottom-left-radius:var(--hq-card-r);border-bottom-right-radius:var(--hq-card-r)}
[data-overlay-surface] main :is(section,section > div) > .py-2 + .flex.justify-between.gap-3.py-2::before {content:'';position:absolute;top:0;left:16px;right:0;border-top:1px solid var(--hq-sep)}
[data-overlay-surface] main :is(section,section > div) > .flex.justify-between.gap-3.py-2 .min-w-0 > :first-child {font-size:15px;font-weight:400}
[data-overlay-surface] main :is(section,section > div) > .flex.justify-between.gap-3.py-2 .min-w-0 > :last-child:not(:first-child) {font-size:13px;color:var(--hq-label-2)}

/* Command Center's Usage: the three totals as tiles, the chart and the top lists as cards. */
[data-overlay-surface] main .grid.grid-cols-2.py-2:has(> .min-w-0 > .tracking-\\[0\\.12em\\]) {gap:12px}
[data-overlay-surface] main .grid.grid-cols-2.py-2 > .min-w-0:has(> .tracking-\\[0\\.12em\\]) {padding:12px 16px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main .tracking-\\[0\\.12em\\].uppercase {font-size:13px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--hq-label-2)}
[data-overlay-surface] main .tracking-\\[0\\.12em\\] + * {font-size:22px;font-weight:700;letter-spacing:-.01em}
[data-overlay-surface] main section:has(> .mb-2 > .tracking-\\[0\\.08em\\]) {padding:12px 16px 14px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main :is(section > .mb-2,section) > .tracking-\\[0\\.08em\\].uppercase {font-size:13px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--hq-label-2)}
[data-overlay-surface] main section > .mb-1\\.5.tracking-\\[0\\.08em\\] {padding-inline:16px}
[data-overlay-surface] main section > ul:has(> li.py-1\\.5) {border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] main section > ul > li.py-1\\.5 {position:relative;min-height:44px;padding:8px 16px}
[data-overlay-surface] main section > ul > li.py-1\\.5 + li::before {content:'';position:absolute;top:0;left:16px;right:0;border-top:1px solid var(--hq-sep)}
[data-overlay-surface] main section > ul > li.py-1\\.5 > :first-child {font-size:13px}
[data-overlay-surface] main section > ul > li.py-1\\.5 > :last-child {font-size:12px;color:var(--hq-label-2)}

/* Page tabs (Capabilities, Artifacts, Messaging) are iOS's segmented control; boxes on a page are filled cards, not
   outlines. */
div:has(> button.group\\/text-tab) {gap:0;padding:2px;border-radius:9px;background:var(--hq-fill-3)}
button.group\\/text-tab {height:26px;padding-inline:12px;border-radius:7px;font-size:13px;color:var(--foreground)}
button.group\\/text-tab > span {text-decoration:none !important}
button.group\\/text-tab > span + span {color:var(--hq-label-2)}
button.group\\/text-tab[data-active='true'] {background:var(--hq-cell) !important;font-weight:600;box-shadow:0 3px 8px rgb(0 0 0/.12),0 1px 1px rgb(0 0 0/.04)}
html:root.dark button.group\\/text-tab[data-active='true'] {background:#636366 !important}
/* The small segmented control (Settings choices): same iOS track and raised segment. */
.inline-grid.grid-flow-col.rounded-\\[5px\\]:has(> button[aria-pressed]) {gap:0;padding:2px;border-radius:9px;background:var(--hq-fill-3)}
.inline-grid.grid-flow-col.rounded-\\[5px\\] > button[aria-pressed] {padding:3px 12px;border-radius:7px;font-size:12px}
.inline-grid.grid-flow-col.rounded-\\[5px\\] > button[aria-pressed='true'] {background:#fff;color:#000;box-shadow:var(--hq-thumb-shadow)}
html:root.dark .inline-grid.grid-flow-col.rounded-\\[5px\\] > button[aria-pressed='true'] {background:#636366;color:#fff}
:is([class~='border-(--ui-stroke-tertiary)'][class~='bg-(--ui-bg-quinary)'],.rounded-xl.border[class~='bg-(--ui-surface-secondary,transparent)'],article.group\\/artifact) {
  border-color:transparent;border-radius:var(--hq-card-r);background:var(--hq-card)}

/* Inside a sheet the page is already grey, so a filled box is a white (dark: raised) cell instead. */
[data-overlay-surface] :is([class~='border-(--ui-stroke-tertiary)'][class~='bg-(--ui-bg-quinary)'],.rounded-xl.border[class~='bg-(--ui-surface-secondary,transparent)']) {background:var(--hq-cell)}
/* Command Center's System: a title-sized status, sentence-case labels, the log in a cell at 12px. */
[data-overlay-surface] main .flex.items-center.gap-2 > .size-2.rounded-full + span, [data-overlay-surface] main .flex.items-center.gap-2:has(> .rounded-full) > span.font-medium {font-size:17px;font-weight:600}
[data-overlay-surface] main .group\\/logs {padding:4px}
[data-overlay-surface] main .group\\/logs .font-mono {font-size:12px;line-height:1.55}
[data-overlay-surface] main .pt-2 > .mb-2 > .tracking-\\[0\\.08em\\].uppercase {padding-inline:16px;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--hq-label-2)}
/* Usage chart: input in HQ's tint, output in its green, bars with soft tops. */
[data-overlay-surface] main [class*='bg-[color:var(--dt-primary)]'] {background:var(--hq-tint);opacity:.85}
[data-overlay-surface] main [class*='bg-emerald-500'] {background:var(--hq-green)}
[data-overlay-surface] main .flex.h-24.items-end {gap:3px}
[data-overlay-surface] main .flex.h-24.items-end > * > :first-child {border-radius:3px 3px 0 0}

/* Profiles: roomier rows, the path as a caption, SOUL.md's heading as a footnote and its editor in a cell with no
   dotted focus box. */
.cm-editor.cm-focused {outline:none}
[data-overlay-surface] .group\\/row.h-7 {height:34px;font-size:14px}
[data-overlay-surface] p.font-mono.text-\\[0\\.66rem\\] {font-size:12px;color:var(--hq-label-2)}
[data-overlay-surface] .tracking-\\[0\\.14em\\].uppercase {padding-inline:16px;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--hq-label-2)}
[data-overlay-surface] .tracking-\\[0\\.14em\\] + * {padding-inline:16px;font-size:13px;color:var(--hq-label-2)}
[data-overlay-surface] .flex.h-full.flex-col.overflow-hidden.rounded-md.border[class~='border-(--ui-stroke-tertiary)'] {border-color:transparent;border-radius:var(--hq-card-r);background:var(--hq-cell)}

/* Spawn tree (Agents): each parent's workers in a cell, a footnote heading, 15pt goals; the empty state at title size. */
[data-overlay-surface] .grid.place-items-center:is(.py-12,.py-10) p.text-sm {font-size:17px;font-weight:600}
[data-overlay-surface] .grid.place-items-center:is(.py-12,.py-10) p:is(.text-xs,.text-sm + p) {font-size:13px;color:var(--hq-label-2)}
[data-overlay-surface] .flex-col.gap-4 > p.shrink-0.text-\\[0\\.7rem\\] {padding-inline:16px;font-size:13px;color:var(--hq-label-2)}
[data-overlay-surface] section.grid.min-w-0.gap-3:has(> p.tracking-wider) {gap:10px}
[data-overlay-surface] section.grid.min-w-0.gap-3 > p.tracking-wider {padding-inline:16px;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--hq-label-2)}
[data-overlay-surface] section.grid.min-w-0.gap-3 > p.tracking-wider + .grid {gap:0;padding:4px 16px;border-radius:var(--hq-card-r);background:var(--hq-cell)}
[data-overlay-surface] section.grid.min-w-0.gap-3 > p.tracking-wider + .grid > [data-slot='tool-block'] {padding-block:10px}
[data-overlay-surface] section.grid.min-w-0.gap-3 > p.tracking-wider + .grid > [data-slot='tool-block'] + [data-slot='tool-block'] {box-shadow:inset 0 1px 0 var(--hq-sep)}
[data-overlay-surface] [data-slot='tool-block'] .text-\\[0\\.82rem\\] {font-size:15px;line-height:20px}
[data-overlay-surface] [data-slot='tool-block'] .text-\\[0\\.66rem\\] {font-size:12px;line-height:16px;color:var(--hq-label-2)}
[data-overlay-surface] [data-slot='tool-block'] .text-\\[0\\.72rem\\] {font-size:13px}

/* Group chats: HQ's room. Each bot's words in a grey bubble under its name and face, yours in blue on the right,
   at the chat's 15 px instead of desktop's 12 px log lines. */
:has(> .min-w-0 > [data-slot='group-chat-message-content']) {gap:8px;padding:3px 0;background:none}
:has(> .min-w-0 > [data-slot='group-chat-message-content']) > .shrink-0 > * {width:30px !important;height:30px !important}
:has(> [data-slot='group-chat-message-content']) .text-\\[0\\.7rem\\] {font-size:12px;font-weight:600;padding-left:13px}
:has(> [data-slot='group-chat-message-content']) .text-\\[0\\.625rem\\] {font-size:11px}
[data-slot='group-chat-message-content'] {width:fit-content;max-width:min(88%,44rem);margin-top:2px;padding:7px 13px;
  border-radius:18px;background:var(--hq-bubble-bot);color:var(--foreground);font-size:15px;line-height:1.35}
.bg-\\(--chrome-action-hover\\):has(> .min-w-0 > [data-slot='group-chat-message-content']) [data-slot='group-chat-message-content'] {
  margin-left:auto;background:var(--hq-bubble-me);color:#fff}
/* The room in the same centred column as a chat, its message box the chat's round field at reading size. */
${ROOM} > .overscroll-contain > .grid {width:100%;max-width:var(--composer-width);margin-inline:auto}
${ROOM} > .border-t {border-top-color:transparent;padding:8px 16px 12px}
${ROOM} > .border-t > form {width:100%;max-width:var(--composer-width);margin-inline:auto}
${ROOM} textarea[data-slot='textarea'] {min-height:40px;padding:10px 16px;border-radius:20px;font-size:15px;line-height:20px}
.bg-\\(--chrome-action-hover\\):has(> .min-w-0 > [data-slot='group-chat-message-content']) .flex.items-center.gap-2 {justify-content:flex-end}
.bg-\\(--chrome-action-hover\\):has(> .min-w-0 > [data-slot='group-chat-message-content']) .flex.items-center.gap-2 > .ml-auto {margin-left:0;order:-1}
`

export const THEME = {
  name: 'hermes-hq',
  label: 'Hermes HQ',
  description: 'The Hermes HQ iPhone look: iOS colours, system blue, round shapes',
  typography: { fontSans: SYSTEM_SANS, fontMono: SYSTEM_MONO },
  colors: LIGHT,
  darkColors: DARK,
  customCSS: CSS,
}

// Nous High Contrast: desktop's own Nous palette (@hermes/shared theme-presets.ts, copied: a plugin can't import it)
// with Hermes HQ's High Contrast option (src/phone-ui-contrast.css) on top. Every value below is mixed from the
// theme's own text, page and accent, as the phone does: lines and secondary text pulled toward the text colour, boxes
// lifted off the page, your messages tinted with the accent and outlined. The palette is set inline on <html>, so these
// go on <body>, where everything inherits them.
export const NOUS_LIGHT = {
  background: '#ffffff', foreground: '#1f2328', card: '#f6f8fa', cardForeground: '#1f2328',
  muted: '#f6f6f6', mutedForeground: '#656d76', popover: '#ffffff', popoverForeground: '#1f2328',
  primary: '#0053fd', primaryForeground: '#ffffff', secondary: '#deeaff', secondaryForeground: '#1f2328',
  accent: '#e3edff', accentForeground: '#1f2328', border: '#d0d7de', input: '#ffffff', ring: '#0053fd',
  midground: '#0053fd', midgroundForeground: '#ffffff', composerRing: '#0053fd',
  destructive: '#cf222e', destructiveForeground: '#ffffff',
  sidebarBackground: '#f6f8fa', sidebarBorder: '#d0d7de', userBubble: '#dae7fd', userBubbleBorder: '#d0d7de',
}
export const NOUS_DARK = {
  background: '#0d1117', foreground: '#e6edf3', card: '#010409', cardForeground: '#e6edf3',
  muted: '#1a1e24', mutedForeground: '#7d8590', popover: '#161b22', popoverForeground: '#e6edf3',
  primary: '#4a84fe', primaryForeground: '#161616', secondary: '#1d2e4f', secondaryForeground: '#e6edf3',
  accent: '#17243a', accentForeground: '#e6edf3', border: '#30363d', input: '#0d1117', ring: '#4a84fe',
  midground: '#4a84fe', midgroundForeground: '#161616', composerRing: '#4a84fe',
  destructive: '#f85149', destructiveForeground: '#ffffff',
  sidebarBackground: '#010409', sidebarBorder: '#30363d', userBubble: '#07162c', userBubbleBorder: '#30363d',
}

export const CONTRAST_CSS = `
body {
  --hc-ink:var(--theme-foreground);--hc-page:var(--theme-background-seed);--hc-accent:var(--theme-midground);
  /* Lines: the text colour at a fixed share, about twice the theme's own (harder ones read as too much). */
  --ui-stroke-primary:color-mix(in srgb,var(--hc-ink) 18%,transparent);
  --ui-stroke-secondary:color-mix(in srgb,var(--hc-ink) 13%,transparent);
  --ui-stroke-tertiary:color-mix(in srgb,var(--hc-ink) 10%,transparent);
  --dt-border:var(--ui-stroke-secondary);--dt-input:var(--ui-stroke-primary);--dt-sidebar-border:var(--ui-stroke-tertiary);
  /* Secondary and tertiary text: a step toward the text colour. */
  --ui-text-secondary:color-mix(in oklab,var(--hc-ink) 82%,var(--hc-page));
  --ui-text-tertiary:color-mix(in oklab,var(--hc-ink) 64%,var(--hc-page));
  /* Boxes (cards, code, the message bar's field) lifted clearly off the page. */
  --ui-bg-editor:color-mix(in oklab,var(--hc-ink) 10%,var(--hc-page));
  --ui-bg-card:var(--ui-bg-editor);
  /* A selected row: the accent, so it never reads as just another surface. */
  --ui-row-active-background:color-mix(in oklab,var(--hc-accent) 22%,var(--hc-page));
  /* Your messages: the accent's tint with an accent outline. */
  --dt-user-bubble:color-mix(in oklab,var(--hc-accent) 28%,var(--hc-page));
  --dt-user-bubble-border:color-mix(in oklab,var(--hc-accent) 75%,var(--hc-page));
}
/* Light pages show a tint far more than dark ones: lighter fills, the same lines. */
html:not(.dark) body {
  --ui-bg-editor:color-mix(in oklab,var(--hc-ink) 5%,var(--hc-page));
  --ui-row-active-background:color-mix(in oklab,var(--hc-accent) 14%,var(--hc-page));
  --dt-user-bubble:color-mix(in oklab,var(--hc-accent) 13%,var(--hc-page));
  --dt-user-bubble-border:color-mix(in oklab,var(--hc-accent) 65%,var(--hc-page));
}
/* The chat keeps the strong treatment: your message and the message bar get a full-strength outline. */
[data-slot='aui_user-message-root'] .composer-human-message {border-color:var(--dt-user-bubble-border)}
[data-slot='composer-surface'] {border-color:color-mix(in srgb,var(--hc-ink) 50%,transparent)}
`

const NOUS_SANS = '"Segoe WPC", "Segoe UI", -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", system-ui, sans-serif, "Apple Color Emoji"'
const NOUS_MONO = '"JetBrains Mono", "Cascadia Code", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", "Noto Mono", "SF Mono", ui-monospace, Menlo, Monaco, Consolas, monospace, "Apple Color Emoji"'

export const NOUS_HIGH_CONTRAST = {
  name: 'nous-high-contrast',
  label: 'Nous High Contrast',
  description: 'Nous with Hermes HQ’s High Contrast: clearer lines and text, your messages outlined in blue',
  typography: { fontSans: NOUS_SANS, fontMono: NOUS_MONO },
  colors: NOUS_LIGHT,
  darkColors: NOUS_DARK,
  customCSS: CONTRAST_CSS,
}

// Newer looks without reinstalling. This copy paints first; then, once per launch, the theme reads the newest published
// look (latest.json beside this file on GitHub, written from this file by scripts/desktop-theme-latest.mjs) and
// switches to it when its version is above this copy's. Only data is taken (palette values, fonts and CSS, never code),
// and a look that is malformed or whose CSS would load anything from elsewhere is ignored. The last good look is kept in
// this computer's storage, so the next launch paints it at once, offline too. A published version at or below this
// copy's (a rollback, or a newer copy installed here) drops the kept one. Bump VERSION with every change to the look.
export const VERSION = 4
export const LATEST_URL = 'https://raw.githubusercontent.com/mrcharlesiv/hermes-hq/main/desktop-plugin/hermes-hq-theme/latest.json'
const KEPT = 'hermes-hq-theme.latest'
const IDS = new Set([THEME.name, NOUS_HIGH_CONTRAST.name])
const MAX_CSS = 192 * 1024
// Anything in the CSS that could fetch from elsewhere: url(), @import, image-set(), @font-face, src().
const LOADS = /url\s*\(|@import|image-set\s*\(|@font-face|\bsrc\s*\(/i
const plainText = (value, max) => typeof value === 'string' && value.length <= max && !/[;{}<>]/.test(value)

const palette = value => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const entries = Object.entries(value)
  if (entries.length > 64 || !entries.every(([key, v]) => /^\w{1,40}$/.test(key) && plainText(v, 64))) return null
  return Object.fromEntries(entries)
}

const theme = value => {
  if (!value || typeof value !== 'object' || !IDS.has(value.name) || !plainText(value.label, 80)) return null
  if (value.description != null && !plainText(value.description, 300)) return null
  const colors = palette(value.colors), darkColors = palette(value.darkColors)
  if (!colors || !darkColors) return null
  const typography = value.typography ?? {}
  if (!['fontSans', 'fontMono'].every(key => typography[key] == null || plainText(typography[key], 400))) return null
  const css = value.customCSS ?? ''
  if (typeof css !== 'string' || css.length > MAX_CSS || LOADS.test(css)) return null
  return {
    name: value.name, label: value.label, description: value.description ?? '',
    typography: { fontSans: typography.fontSans, fontMono: typography.fontMono },
    colors, darkColors, customCSS: css,
  }
}

/** A published look as `{version, themes}`, rebuilt from the allowed fields only; null when anything is off. */
export function readLook(value) {
  if (!value || typeof value !== 'object' || !Number.isInteger(value.version) || value.version < 1) return null
  if (!Array.isArray(value.themes) || value.themes.length < 1 || value.themes.length > IDS.size) return null
  const themes = value.themes.map(theme)
  if (themes.some(t => !t) || new Set(themes.map(t => t.name)).size !== themes.length) return null
  return { version: value.version, themes }
}

const kept = () => {
  try { return readLook(JSON.parse(localStorage.getItem(KEPT) ?? 'null')) } catch { return null }
}
const keep = look => {
  try { look ? localStorage.setItem(KEPT, JSON.stringify(look)) : localStorage.removeItem(KEPT) } catch {}
}

export default {
  id: 'hermes-hq-theme',
  name: 'Hermes HQ theme',
  description: 'Adds the Hermes HQ themes: the iPhone app’s look, and Nous with its High Contrast option.',
  register(ctx) {
    const paint = themes => { for (const t of themes) ctx.register({ id: t.name, area: 'themes', title: t.label, data: t }) }
    const bundled = [THEME, NOUS_HIGH_CONTRAST]
    paint(bundled)
    let shown = kept()
    if (shown && shown.version > VERSION) paint(shown.themes)
    else { keep(null); shown = null }

    if (typeof fetch !== 'function') return
    const abort = new AbortController()
    let gone = false
    ctx.onDispose(() => { gone = true; abort.abort() })
    const stop = setTimeout(() => abort.abort(), 15000)
    fetch(LATEST_URL, { cache: 'no-store', signal: abort.signal })
      .then(response => (response.ok ? response.json() : null))
      .then(value => {
        const look = readLook(value)
        if (gone || !look) return
        if (look.version > VERSION) {
          if (JSON.stringify(look) !== JSON.stringify(shown)) paint(look.themes)
          keep(look)
        } else if (shown) {
          paint(bundled)
          keep(null)
        }
      })
      .catch(() => {})
      .finally(() => clearTimeout(stop))
  },
}
