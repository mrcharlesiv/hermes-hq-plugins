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

export const LIGHT = {
  background: '#ffffff', foreground: '#000000', card: '#f2f2f7', cardForeground: '#000000',
  muted: '#f2f2f7', mutedForeground: '#8a8a8e', popover: '#ffffff', popoverForeground: '#000000',
  primary: '#007aff', primaryForeground: '#ffffff', secondary: '#e5f1ff', secondaryForeground: '#000000',
  accent: '#e5f1ff', accentForeground: '#000000', border: '#c6c6c8', input: '#ffffff', ring: '#007aff',
  midground: '#007aff', midgroundForeground: '#ffffff', composerRing: '#007aff',
  destructive: '#ff3b30', destructiveForeground: '#ffffff',
  sidebarBackground: '#f2f2f7', sidebarBorder: '#c6c6c8',
  // Your bubble is the accent, as on the phone (--ios-bubble-me-fill).
  userBubble: '#007aff', userBubbleBorder: '#007aff',
}

export const DARK = {
  background: '#000000', foreground: '#ffffff', card: '#1c1c1e', cardForeground: '#ffffff',
  muted: '#1c1c1e', mutedForeground: '#8d8d93', popover: '#1c1c1e', popoverForeground: '#ffffff',
  primary: '#0a84ff', primaryForeground: '#ffffff', secondary: '#0b2545', secondaryForeground: '#ffffff',
  accent: '#10233b', accentForeground: '#ffffff', border: '#38383a', input: '#1c1c1e', ring: '#0a84ff',
  midground: '#0a84ff', midgroundForeground: '#ffffff', composerRing: '#0a84ff',
  destructive: '#ff453a', destructiveForeground: '#ffffff',
  sidebarBackground: '#1c1c1e', sidebarBorder: '#38383a',
  // The accent a quarter darker, so white text keeps 4.5:1 (the phone's dark --ios-bubble-me-fill).
  userBubble: '#0863bf', userBubbleBorder: '#0863bf',
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
html:root {--hq-bubble-me:#007aff}
html:root.dark {--hq-bubble-me:#0863bf}
${USER} {
  width:fit-content;max-width:min(80%,36rem);margin-left:auto;
  padding:7px 13px;border:0;border-radius:18px;
  background:var(--hq-bubble-me);color:#fff;box-shadow:none;backdrop-filter:none}
${USER} :is(*,a,.ref) {color:inherit}
${USER} a {text-decoration:underline;text-underline-offset:.15em}
[data-slot='aui_user-message-root'] .composer-human-message-container {background:transparent;justify-content:flex-end}

/* A bot's text: a grey bubble on the left, as on the phone (--ios-bubble-bot). Its tool rows stay outside, as the
   reply's work trail. Only the reply's own text blocks: markdown nested in a tool's output keeps its plain look. */
html:root {--hq-bubble-bot:#e9e9eb}
html:root.dark {--hq-bubble-bot:#26282c}
${BOT} {
  width:fit-content;max-width:min(88%,44rem);box-sizing:border-box;
  padding:7px 13px;border-radius:18px;background:var(--hq-bubble-bot)}
${BOT} > div > :first-child {margin-top:0}
${BOT} > div > :last-child {margin-bottom:0}

/* The composer: one round field on the page colour with a hairline, as the phone's message box. */
body {--composer-fill:var(--hq-cell)}
[data-slot='composer-surface'] {border-radius:22px;border-color:var(--hq-sep)}
[data-slot='composer-rich-input'] {font-size:15px;line-height:20px}

/* Code and cards on the phone's card shape. */
:is([data-slot='code-card'],[data-slot='file-diff-panel'],.aui-md pre) {border-radius:14px}
.aui-md :not(pre) > code {border-radius:6px}

/* ---- The rest of the app: iOS's colours and shapes on desktop's shared parts. ---- */

/* iOS's system values (phone-ios-tokens.css), on <body> so they sit under the palette desktop sets on <html>. */
body {
  --hq-cell:#fff;--hq-grouped:#f2f2f7;--hq-raised:#fff;
  --hq-sep:rgb(60 60 67/.29);--hq-label-2:rgb(60 60 67/.6);
  --hq-fill:rgb(120 120 128/.2);--hq-fill-2:rgb(120 120 128/.16);--hq-fill-3:rgb(118 118 128/.12);
  --hq-green:#34c759;--hq-thumb-shadow:0 3px 8px rgb(0 0 0/.15),0 3px 1px rgb(0 0 0/.06);
  --hq-material:rgb(250 250 250/.8);--hq-menu-shadow:0 12px 40px rgb(0 0 0/.16),0 0 0 .5px rgb(0 0 0/.08);
  --hq-tint:var(--theme-primary)}
html:root.dark body {
  --hq-cell:#1c1c1e;--hq-grouped:#000;--hq-raised:#2c2c2e;
  --hq-sep:rgb(84 84 88/.6);--hq-label-2:rgb(235 235 245/.6);
  --hq-fill:rgb(120 120 128/.36);--hq-fill-2:rgb(120 120 128/.32);--hq-fill-3:rgb(118 118 128/.24);
  --hq-green:#30d158;--hq-thumb-shadow:0 3px 8px rgb(0 0 0/.3),0 3px 1px rgb(0 0 0/.1);
  --hq-material:rgb(37 37 40/.82);--hq-menu-shadow:0 12px 40px rgb(0 0 0/.5),0 0 0 .5px rgb(255 255 255/.1)}

/* Sentence case everywhere: iOS never spaces out capitals for a heading or a tab. */
html:root body .uppercase {text-transform:none;letter-spacing:normal}

/* Buttons: capsules, as iOS 26 draws them. A text button is the tint, never underlined; a grey button has tint text. */
[data-slot='button']:not([data-size='inline'],[data-size='micro'],[data-size='grip'],[data-variant='link'],[data-variant='text'],[data-variant='textStrong']) {border-radius:999px}
[data-slot='button'][data-variant='default'] {font-weight:600}
[data-slot='button']:is([data-variant='secondary'],[data-variant='outline']) {
  background:var(--hq-fill-3);color:var(--hq-tint);box-shadow:none}
[data-slot='button']:is([data-variant='secondary'],[data-variant='outline']):hover {background:var(--hq-fill-2);color:var(--hq-tint)}
[data-slot='button']:is([data-variant='link'],[data-variant='text'],[data-variant='textStrong']) {color:var(--hq-tint);text-decoration:none}
[data-slot='button']:is([data-variant='link'],[data-variant='text'],[data-variant='textStrong']):hover {color:var(--hq-tint);opacity:.7;text-decoration:none}

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

/* Menus: iOS's frosted menu with round corners and rounded highlights. */
:is([data-slot='dropdown-menu-content'],[data-slot='dropdown-menu-sub-content'],[data-slot='context-menu-content'],[data-slot='context-menu-sub-content'],[data-slot='select-content'],[data-slot='command']) {
  border-radius:14px;border-color:transparent;background:var(--hq-material);
  backdrop-filter:blur(30px) saturate(180%);box-shadow:var(--hq-menu-shadow)}
:is([data-slot='dropdown-menu-item'],[data-slot='dropdown-menu-checkbox-item'],[data-slot='dropdown-menu-radio-item'],[data-slot='dropdown-menu-sub-trigger'],[data-slot='context-menu-item'],[data-slot='context-menu-checkbox-item'],[data-slot='context-menu-sub-trigger'],[data-slot='select-item'],[data-slot='command-item']) {border-radius:8px}
:is([data-slot='dropdown-menu-separator'],[data-slot='context-menu-separator'],[data-slot='select-separator'],[data-slot='command-separator']) {background:var(--hq-sep)}
[data-slot='popover-content'] {border-radius:14px;border-color:transparent;background:var(--hq-raised);box-shadow:var(--hq-menu-shadow)}
[data-slot='tooltip-content'] {border-radius:8px}
:is([data-slot='badge'],[data-slot='kbd']) {border-radius:6px}

/* Dialogs and the big pages that open over the chat (Settings, Jobs, Profiles…): a light dim, no blur, and a
   card with the phone's sheet corners. */
:is([data-slot='dialog-overlay'],[data-slot='sheet-overlay'],[data-overlay-surface]) {background-color:rgb(0 0 0/.2);backdrop-filter:none}
html:root.dark :is([data-slot='dialog-overlay'],[data-slot='sheet-overlay'],[data-overlay-surface]) {background-color:rgb(0 0 0/.48)}
[data-slot='dialog-content'] {border-radius:22px;border-color:transparent;box-shadow:0 20px 60px rgb(0 0 0/.22)}
[data-overlay-surface] [data-glass-raised] {border-radius:22px;border-color:transparent;background-color:var(--hq-cell);box-shadow:0 20px 60px rgb(0 0 0/.22)}

/* Settings: iOS's grouped list. Grey page, each group of options a white card, hairlines between rows. */
[data-overlay-surface] [data-glass-raised]:has([id^='setting-field-']) :is(main,div:has(> main)) {background:var(--hq-grouped)}
:has(> [id^='setting-field-']) {gap:0;padding:0 16px;border-radius:12px;background:var(--hq-cell)}
[id^='setting-field-'] {border-radius:0}
[id^='setting-field-'] + [id^='setting-field-'] {box-shadow:0 -.5px 0 var(--hq-sep)}

/* The sidebar: round faces, list type a little larger, quiet section headings, no tree lines. */
[data-slot='row-button'] {border-radius:10px}
[data-slot='bots-roster'] img {border-radius:50% !important}   /* desktop sets 22% inline */
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] .font-medium {font-size:14px;font-weight:600}
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] .text-xs {font-size:12.5px}
[data-slot='bots-roster'] button.text-\\[0\\.6875rem\\] {font-size:12px;font-weight:600;color:var(--hq-label-2)}
[data-slot='bots-section'] {border-left-color:transparent}

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

/* Bot rows: HQ's face, gap and padding (50 pt faces, 12 pt gap, 10 pt padding), names in the headline weight, a
   larger preview. Group rows' icons take the same circle. */
[data-slot='bots-roster'] [data-slot='context-menu-trigger']:has(> .shrink-0) {gap:12px;padding:8px 10px;min-height:62px}
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] img {width:44px !important;height:44px !important}   /* sized inline */
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] > .shrink-0:has(> svg), [data-slot='bots-roster'] [data-slot='context-menu-trigger'] > .shrink-0 > svg {width:44px;height:44px}   /* drawn faces */
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] .w-\\[34px\\] {width:44px}
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] .size-8 {width:44px;height:44px;border-radius:50%}
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] .font-medium {font-size:15px;font-weight:600}
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] .text-xs {font-size:13px;line-height:18px}
[data-slot='bots-roster'] [data-slot='context-menu-trigger'] .text-\\[0\\.6875rem\\] {font-size:12px}
[data-slot='bots-roster'] [data-slot='bots-section-heading'] {margin-top:8px}

/* The sidebar's search: HQ's grey capsule field instead of an underlined line. */
[data-slot='sidebar-wrapper'] .border-b:has(> input[placeholder^='Search']) {
  display:flex;width:100%;height:32px;padding:0 10px;border:0;border-radius:10px;background:var(--hq-fill-3);opacity:1}
[data-slot='sidebar-wrapper'] .border-b:has(> input[placeholder^='Search']) > input {font-size:13px;field-sizing:fixed}

/* Tabs a size up, with room round the label. */
:is([data-slot='pane-tab'],[role='tab'][data-tree-tab]) {font-size:13px}
:is([data-slot='pane-tab'],[role='tab'][data-tree-tab]):not([data-vertical]) {height:24px;padding-inline:4px}

/* Settings: the options in a readable centred column, as iPad Settings, on cards with HQ's round corners; the list
   of sections down the side at iOS's row height. */
[data-overlay-surface] [data-glass-raised]:has([id^='setting-field-']) main > * {width:100%;max-width:54rem;margin-inline:auto}
:has(> [id^='setting-field-']) {border-radius:20px}
[id^='setting-field-'] > * > * {min-height:44px}
[data-overlay-surface] aside button:not([data-slot='button']) {height:auto;min-height:34px;font-size:14px;border-radius:10px;border-color:transparent}

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

export default {
  id: 'hermes-hq-theme',
  name: 'Hermes HQ theme',
  description: 'Adds the Hermes HQ themes: the iPhone app’s look, and Nous with its High Contrast option.',
  register(ctx) {
    ctx.register({ id: 'hermes-hq', area: 'themes', title: THEME.label, data: THEME })
    ctx.register({ id: 'nous-high-contrast', area: 'themes', title: NOUS_HIGH_CONTRAST.label, data: NOUS_HIGH_CONTRAST })
  },
}
