import { useEffect, useMemo, useRef, useState } from 'react'
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from '@tanstack/react-table'
import { TransformComponent, TransformWrapper } from 'react-zoom-pan-pinch'
import './index.css'
import type {
  ArtifactEntry,
  LibraryPayload,
  ReviewerPayload,
  SessionFrame,
  SessionLibraryEntry,
  SessionPayload,
  ThemeMode,
  ThemeSpec,
  TraceEvent,
  TurnSummary,
} from './types'
import { useClampedText } from './usePretextClamp'

type DetailTab = 'focus' | 'artifacts'

const BODY_FONT = '500 14px Inter, "Segoe UI", Arial, sans-serif'
const SMALL_FONT = '500 13px Inter, "Segoe UI", Arial, sans-serif'
const ARTBOARD_BUTTON_ZOOM_STEP = 0.18
const ARTBOARD_WHEEL_STEP = 0.018

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0s'
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes}m ${String(remainder).padStart(2, '0')}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

function formatStamp(value: string): string {
  const stamp = new Date(value)
  if (Number.isNaN(stamp.getTime())) return value
  return stamp.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function eventTone(type: string): string {
  if (type.includes('error') || type.includes('fail') || type.includes('cancel')) return 'danger'
  if (type.includes('browser') || type.includes('vision') || type.includes('holonet') || type.includes('tool')) {
    return 'accent'
  }
  if (type.includes('task') || type.includes('subagent') || type.includes('progress')) return 'warm'
  if (type.includes('user') || type.includes('submit')) return 'ink'
  return 'neutral'
}

function summarizeEvent(event: TraceEvent): string {
  if (typeof event.assistant_excerpt === 'string' && event.assistant_excerpt.trim()) {
    return event.assistant_excerpt.trim()
  }
  if (typeof event.note === 'string' && event.note.trim()) {
    return event.note.trim()
  }
  if (typeof event.route === 'string' && event.route.trim()) {
    return `${String(event.type ?? 'event')} · ${event.route}`
  }
  if (typeof event.verb === 'string' && event.verb.trim()) {
    return `${String(event.type ?? 'event')} · ${event.verb}`
  }
  return String(event.type ?? 'event').replaceAll('_', ' ')
}

function summarizeTurn(turn: TurnSummary): string {
  const parts = [
    formatDuration(turn.duration_s),
    `${turn.frame_count} frames`,
    turn.max_agent_turn ? `agent ${turn.max_agent_turn}` : '',
    turn.max_running_tools ? `${turn.max_running_tools} tools` : '',
  ].filter(Boolean)
  return parts.join(' · ')
}

function pickPreviewFrame(frames: SessionFrame[]): number {
  if (!frames.length) return 0
  const reversed = [...frames].reverse()
  const found = reversed.find(
    (frame) => frame.running_tools > 0 || frame.stream_open || frame.turn_index > 0 || frame.agent_turn > 1,
  )
  if (!found) return frames.length - 1
  return frames.findIndex((frame) => frame.index === found.index)
}

function closestFrameForTrace(frames: SessionFrame[], target: number): number {
  if (!frames.length) return 0
  let bestIndex = 0
  let bestDelta = Number.POSITIVE_INFINITY
  frames.forEach((frame, index) => {
    const delta = Math.abs(frame.scenario_elapsed_s - target)
    if (delta < bestDelta) {
      bestDelta = delta
      bestIndex = index
    }
  })
  return bestIndex
}

function frameForTurn(frames: SessionFrame[], turnIndex: number): number {
  const found = frames.findIndex((frame) => frame.turn_index === turnIndex)
  return found >= 0 ? found : 0
}

function findFrameForElapsed(frames: SessionFrame[], elapsed: number): number {
  if (!frames.length) return 0
  let low = 0
  let high = frames.length - 1
  let best = 0
  while (low <= high) {
    const mid = Math.floor((low + high) / 2)
    const value = frames[mid]?.scenario_elapsed_s ?? 0
    if (value <= elapsed) {
      best = mid
      low = mid + 1
    } else {
      high = mid - 1
    }
  }
  return best
}

function firstImage(artifacts: SessionPayload['artifacts']): ArtifactEntry | null {
  return artifacts.images[0] ?? null
}

function collectArtifacts(artifacts: SessionPayload['artifacts']): ArtifactEntry[] {
  return [...artifacts.primary, ...artifacts.turn_files, ...artifacts.images]
}

function clamp(input: string, limit: number): string {
  if (input.length <= limit) return input
  return `${input.slice(0, Math.max(0, limit - 1))}…`
}

function useThemeState(payload: ReviewerPayload) {
  const themes = useMemo(
    () => new Map(payload.theme_catalog.map((theme) => [theme.name, theme])),
    [payload.theme_catalog],
  )
  const defaultTheme = payload.default_theme || payload.theme_catalog[0]?.name || 'paper'
  const defaultMode: ThemeMode = payload.default_mode || 'light'
  const [themeName, setThemeName] = useState(() => window.localStorage.getItem('successor-reviewer-theme') || defaultTheme)
  const [mode, setMode] = useState<ThemeMode>(() => {
    const persisted = window.localStorage.getItem('successor-reviewer-mode')
    return persisted === 'dark' ? 'dark' : persisted === 'light' ? 'light' : defaultMode
  })

  const theme = themes.get(themeName) ?? themes.get(defaultTheme) ?? payload.theme_catalog[0]

  useEffect(() => {
    if (!theme) return
    const palette = mode === 'dark' ? theme.dark : theme.light
    const root = document.documentElement
    root.dataset.themeName = theme.name
    root.dataset.themeMode = mode
    root.style.setProperty('--theme-bg', palette.bg)
    root.style.setProperty('--theme-bg-input', palette.bg_input)
    root.style.setProperty('--theme-bg-footer', palette.bg_footer)
    root.style.setProperty('--theme-fg', palette.fg)
    root.style.setProperty('--theme-fg-dim', palette.fg_dim)
    root.style.setProperty('--theme-fg-subtle', palette.fg_subtle)
    root.style.setProperty('--theme-accent', palette.accent)
    root.style.setProperty('--theme-warm', palette.accent_warm)
    root.style.setProperty('--theme-warn', palette.accent_warn)
    window.localStorage.setItem('successor-reviewer-theme', theme.name)
    window.localStorage.setItem('successor-reviewer-mode', mode)
  }, [mode, theme])

  return { mode, setMode, theme, themeName: theme?.name ?? defaultTheme, setThemeName }
}

function ThemePicker({
  themes,
  mode,
  onModeChange,
  onThemeChange,
  themeName,
}: {
  themes: ThemeSpec[]
  themeName: string
  mode: ThemeMode
  onThemeChange: (name: string) => void
  onModeChange: (mode: ThemeMode) => void
}) {
  return (
    <div className="theme-controls">
      <div className="theme-strip">
        {themes.map((theme) => (
          <button
            key={theme.name}
            type="button"
            className={theme.name === themeName ? 'theme-pill is-active' : 'theme-pill'}
            onClick={() => onThemeChange(theme.name)}
          >
            <span>{theme.icon}</span>
            {theme.name}
          </button>
        ))}
      </div>
      <div className="theme-strip">
        <button
          type="button"
          className={mode === 'light' ? 'theme-pill is-active' : 'theme-pill'}
          onClick={() => onModeChange('light')}
        >
          light
        </button>
        <button
          type="button"
          className={mode === 'dark' ? 'theme-pill is-active' : 'theme-pill'}
          onClick={() => onModeChange('dark')}
        >
          dark
        </button>
      </div>
    </div>
  )
}

function BalancedText({
  text,
  className,
  maxLines,
  font = BODY_FONT,
  lineHeight = 20,
}: {
  text: string
  className?: string
  maxLines: number
  font?: string
  lineHeight?: number
}) {
  const [ref, clamped] = useClampedText(text, font, lineHeight, maxLines)
  return (
    <div ref={ref} className={className} style={{ whiteSpace: 'pre-line' }}>
      {clamped}
    </div>
  )
}

function TerminalViewport({
  frame,
  viewportHeight,
}: {
  frame: SessionFrame | null
  viewportHeight: number
}) {
  const content = frame?.plain ?? ''
  return (
    <div className="terminal-window">
      <div className="terminal-titlebar">
        <div className="window-dots">
          <span />
          <span />
          <span />
        </div>
        <div className="terminal-title">
          {frame ? `turn ${frame.turn_index || 'setup'} · frame ${frame.index}` : 'terminal stage'}
        </div>
      </div>
      <div className="terminal-body">
        <div className="terminal-viewport" aria-label="Session terminal viewport" style={{ minHeight: `${viewportHeight}px` }}>
          <pre className="terminal-pre">{content}</pre>
        </div>
      </div>
    </div>
  )
}

function TerminalArtboard({
  frame,
  turnSummary,
  selectedEvent,
}: {
  frame: SessionFrame | null
  turnSummary: TurnSummary | null
  selectedEvent: TraceEvent | null
}) {
  const lines = useMemo(() => (frame?.plain ?? '').split('\n'), [frame?.plain])
  const rowCount = Math.max(24, lines.length || 1)
  const colCount = Math.max(72, ...lines.map((line) => line.length))
  const artboardWidth = Math.max(920, Math.min(1560, colCount * 8.4 + 88))
  const viewportHeight = Math.max(440, Math.min(820, rowCount * 16.4 + 18))
  const eventTitle = String(selectedEvent?.type ?? 'session frame').replaceAll('_', ' ')
  const eventSummary = selectedEvent ? summarizeEvent(selectedEvent) : 'No trace event selected.'
  const focusMeta = `${eventTitle} · ${eventSummary}`

  return (
    <div className="artboard-stage">
      <TransformWrapper
        minScale={0.45}
        initialScale={0.72}
        limitToBounds={false}
        smooth={false}
        wheel={{ step: ARTBOARD_WHEEL_STEP }}
      >
        {({ resetTransform, zoomIn, zoomOut }) => (
          <>
            <TransformComponent wrapperClass="artboard-wrapper" contentClass="artboard-transform">
              <div className="artboard-sheet" style={{ width: `${artboardWidth}px` }}>
                <div className="artboard-head">
                  <div className="artboard-context">
                    <div className="artboard-context-chip artboard-context-chip-hint">
                      <span>Viewport</span>
                      <strong>Wheel to zoom. Drag to pan.</strong>
                    </div>
                    <div className="artboard-context-chip">
                      <span>Frame</span>
                      <strong>
                        {frame
                          ? `turn ${frame.turn_index || 'setup'} · frame ${frame.index} · ${formatDuration(frame.scenario_elapsed_s)}`
                          : 'No frame loaded.'}
                      </strong>
                    </div>
                    <div className="artboard-context-chip">
                      <span>Turn</span>
                      <strong>{turnSummary ? `turn ${turnSummary.turn_index} · ${summarizeTurn(turnSummary)}` : 'No turn selected.'}</strong>
                    </div>
                    <div className="artboard-context-chip artboard-context-chip-focus">
                      <span>Focus</span>
                      <strong title={focusMeta}>{clamp(focusMeta, 128)}</strong>
                    </div>
                  </div>
                  <div className="artboard-actions">
                    <button type="button" onClick={() => zoomOut(ARTBOARD_BUTTON_ZOOM_STEP)}>−</button>
                    <button type="button" onClick={() => resetTransform()}>reset</button>
                    <button type="button" onClick={() => zoomIn(ARTBOARD_BUTTON_ZOOM_STEP)}>+</button>
                  </div>
                </div>
                <div className="terminal-stage-shell" style={{ minHeight: `${viewportHeight}px` }}>
                  <TerminalViewport frame={frame} viewportHeight={viewportHeight} />
                </div>
              </div>
            </TransformComponent>
          </>
        )}
      </TransformWrapper>
    </div>
  )
}

function LibrarySessionCell({
  session,
}: {
  session: SessionLibraryEntry
}) {
  return (
    <div className="cell-session">
      <strong>{session.title}</strong>
      <span className="cell-session-copy">{session.description}</span>
    </div>
  )
}

function LibraryBase({
  payload,
  themes,
  themeName,
  mode,
  onThemeChange,
  onModeChange,
}: {
  payload: LibraryPayload
  themes: ThemeSpec[]
  themeName: string
  mode: ThemeMode
  onThemeChange: (name: string) => void
  onModeChange: (mode: ThemeMode) => void
}) {
  const [query, setQuery] = useState('')
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return payload.sessions
    return payload.sessions.filter((session) => {
      return [
        session.title,
        session.description,
        session.status,
        session.status_reason,
        session.preview_excerpt,
        session.tools.join(' '),
        session.event_types.join(' '),
      ]
        .join(' ')
        .toLowerCase()
        .includes(needle)
    })
  }, [payload.sessions, query])

  const [selectedSlug, setSelectedSlug] = useState(filtered[0]?.slug ?? payload.sessions[0]?.slug ?? '')

  useEffect(() => {
    if (!filtered.length) return
    if (!filtered.some((entry) => entry.slug === selectedSlug)) {
      setSelectedSlug(filtered[0]?.slug ?? '')
    }
  }, [filtered, selectedSlug])

  const selected = filtered.find((entry) => entry.slug === selectedSlug) ?? filtered[0] ?? payload.sessions[0] ?? null
  const column = createColumnHelper<SessionLibraryEntry>()
  const columns = useMemo(
    () => [
      column.accessor('title', {
        header: 'Session',
        cell: (info) => <LibrarySessionCell session={info.row.original} />,
      }),
      column.accessor('updated_at', {
        header: 'Updated',
        cell: (info) => formatStamp(info.getValue()),
      }),
      column.accessor('duration_s', {
        header: 'Duration',
        cell: (info) => formatDuration(info.getValue()),
      }),
      column.accessor('turn_count', {
        header: 'Turns',
      }),
      column.accessor('max_agent_turn', {
        header: 'Peak',
        cell: (info) => `agent ${info.getValue()}`,
      }),
      column.accessor('status', {
        header: 'Status',
        cell: (info) => <span className={`status-chip tone-${info.row.original.status.toLowerCase()}`}>{info.getValue()}</span>,
      }),
      column.display({
        id: 'tools',
        header: 'Tools',
        cell: (info) => (
          <div className="grid-tools">
            {info.row.original.tools.slice(0, 3).map((tool) => (
              <span key={tool} className="grid-token">{tool}</span>
            ))}
          </div>
        ),
      }),
    ],
    [column],
  )

  const table = useReactTable({
    data: filtered,
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  return (
    <div className="manager-shell manager-shell-library">
      <header className="manager-topbar">
        <div className="topbar-copy">
          <div className="mono-kicker">Successor recordings library</div>
          <h1>Recordings</h1>
          <BalancedText
            text="Search local runs and open playback."
            className="topbar-copy-body"
            maxLines={2}
          />
        </div>
        <ThemePicker
          themes={themes}
          themeName={themeName}
          mode={mode}
          onThemeChange={onThemeChange}
          onModeChange={onModeChange}
        />
      </header>

      <main className="library-layout">
        <section className="library-grid-panel">
          <div className="library-toolbar">
            <input
              className="library-search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter by title, issue, tool, or event type…"
            />
            <div className="library-meta">
              <span>{filtered.length} visible</span>
              <span>{payload.sessions.length} total</span>
            </div>
          </div>

          <div className="grid-wrap">
            <table className="session-grid">
              <thead>
                {table.getHeaderGroups().map((group) => (
                  <tr key={group.id}>
                    {group.headers.map((header) => (
                      <th key={header.id}>
                        {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {table.getRowModel().rows.map((row) => {
                  const active = row.original.slug === selectedSlug
                  return (
                    <tr
                      key={row.id}
                      className={active ? 'is-selected' : undefined}
                      onClick={() => setSelectedSlug(row.original.slug)}
                      onDoubleClick={() => {
                        window.location.href = row.original.href
                      }}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                      ))}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="library-inspector">
          <div className="inspector-head">
            <div className="mono-kicker">Selected Session</div>
            <strong>{selected?.title ?? 'No session selected'}</strong>
          </div>
          {selected ? (
            <>
              <BalancedText
                text={selected.preview_excerpt || selected.description}
                className="inspector-copy"
                font={SMALL_FONT}
                lineHeight={20}
                maxLines={5}
              />
              <dl className="inspector-stats">
                <div><dt>Updated</dt><dd>{formatStamp(selected.updated_at)}</dd></div>
                <div><dt>Duration</dt><dd>{formatDuration(selected.duration_s)}</dd></div>
                <div><dt>Frames</dt><dd>{selected.frame_count}</dd></div>
                <div><dt>Trace Events</dt><dd>{selected.trace_event_count}</dd></div>
                <div><dt>Status</dt><dd>{selected.status_reason}</dd></div>
                <div><dt>Visuals</dt><dd>{selected.visuals_count}</dd></div>
              </dl>
              <div className="inspector-group">
                <div className="mono-kicker">Tools</div>
                <div className="token-row">
                  {selected.tools.map((tool) => (
                    <span key={tool} className="token">{tool}</span>
                  ))}
                </div>
              </div>
              <div className="inspector-group">
                <div className="mono-kicker">Event Types</div>
                <div className="token-row">
                  {selected.event_types.slice(0, 8).map((type) => (
                    <span key={type} className="token subtle">{type}</span>
                  ))}
                </div>
              </div>
              <div className="inspector-actions">
                <button type="button" className="action-primary" onClick={() => { window.location.href = selected.href }}>
                  Open session
                </button>
                {selected.index_href ? (
                  <button type="button" className="action-secondary" onClick={() => { window.location.href = selected.index_href! }}>
                    Open bundle index
                  </button>
                ) : null}
              </div>
            </>
          ) : (
            <div className="inspector-copy">Nothing matches the current filter.</div>
          )}
        </aside>
      </main>
    </div>
  )
}

function SessionWorkbench({
  payload,
  themes,
  themeName,
  mode,
  onThemeChange,
  onModeChange,
}: {
  payload: SessionPayload
  themes: ThemeSpec[]
  themeName: string
  mode: ThemeMode
  onThemeChange: (name: string) => void
  onModeChange: (mode: ThemeMode) => void
}) {
  const [selectedFrameIndex, setSelectedFrameIndex] = useState(() => pickPreviewFrame(payload.frames))
  const [selectedTurn, setSelectedTurn] = useState(payload.turns[0] ?? 0)
  const [selectedEventIndex, setSelectedEventIndex] = useState(0)
  const [traceFilter, setTraceFilter] = useState('')
  const [playing, setPlaying] = useState(false)
  const [detailTab, setDetailTab] = useState<DetailTab>('focus')
  const playbackAnchorRef = useRef<{ wallTimeMs: number; startElapsed: number } | null>(null)

  useEffect(() => {
    const frame = payload.frames[selectedFrameIndex]
    if (frame?.turn_index) setSelectedTurn(frame.turn_index)
  }, [payload.frames, selectedFrameIndex])

  useEffect(() => {
    if (!payload.trace_events.length) {
      setSelectedEventIndex(0)
      return
    }
    if (selectedEventIndex > payload.trace_events.length - 1) {
      setSelectedEventIndex(0)
    }
  }, [payload.trace_events.length, selectedEventIndex])

  const filteredEvents = useMemo(() => {
    const needle = traceFilter.trim().toLowerCase()
    if (!needle) return payload.trace_events
    return payload.trace_events.filter((event) => JSON.stringify(event).toLowerCase().includes(needle))
  }, [payload.trace_events, traceFilter])

  useEffect(() => {
    if (!filteredEvents.length) {
      setSelectedEventIndex(0)
      return
    }
    if (selectedEventIndex > filteredEvents.length - 1) {
      setSelectedEventIndex(0)
    }
  }, [filteredEvents, selectedEventIndex])

  useEffect(() => {
    if (!playing || payload.frames.length < 2) {
      playbackAnchorRef.current = null
      return
    }
    const startFrame = payload.frames[selectedFrameIndex] ?? payload.frames[0]
    if (!startFrame) return
    const lastElapsed = payload.frames[payload.frames.length - 1]?.scenario_elapsed_s ?? 0
    playbackAnchorRef.current = {
      wallTimeMs: window.performance.now(),
      startElapsed: startFrame.scenario_elapsed_s,
    }
    let raf = 0
    const tick = (now: number) => {
      const anchor = playbackAnchorRef.current
      if (!anchor) return
      const elapsed = anchor.startElapsed + (now - anchor.wallTimeMs) / 1000
      const nextIndex = findFrameForElapsed(payload.frames, elapsed)
      setSelectedFrameIndex((current) => (current === nextIndex ? current : nextIndex))
      if (elapsed >= lastElapsed) {
        setSelectedFrameIndex(payload.frames.length - 1)
        setPlaying(false)
        playbackAnchorRef.current = null
        return
      }
      raf = window.requestAnimationFrame(tick)
    }
    raf = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(raf)
  }, [payload.frames, playing])

  const selectedFrame = payload.frames[selectedFrameIndex] ?? null
  const selectedEvent = filteredEvents[selectedEventIndex] ?? filteredEvents[0] ?? null
  const selectedTurnSummary =
    payload.turn_summaries.find((turn) => turn.turn_index === selectedTurn) ?? payload.turn_summaries[0] ?? null
  const imageArtifact = firstImage(payload.artifacts)
  const allArtifacts = collectArtifacts(payload.artifacts)
  const selectedEventJson = selectedEvent ? JSON.stringify(selectedEvent, null, 2) : ''
  const eventSelectionMeta =
    typeof selectedEvent?.t === 'number' ? `${formatDuration(selectedEvent.t)} · ${String(selectedEvent.type ?? 'event')}` : 'No trace time'

  const jumpToTurn = (turnIndex: number) => {
    setPlaying(false)
    setSelectedTurn(turnIndex)
    setSelectedFrameIndex(frameForTurn(payload.frames, turnIndex))
  }

  const selectEvent = (event: TraceEvent, index: number) => {
    setSelectedEventIndex(index)
    setPlaying(false)
    setDetailTab('focus')
    if (typeof event.t === 'number') {
      setSelectedFrameIndex(closestFrameForTrace(payload.frames, event.t))
    }
  }

  return (
    <div className="manager-shell manager-shell-session">
      <header className="manager-topbar">
        <div className="topbar-copy">
          <div className="mono-kicker">Successor session review</div>
          <h1>{payload.title}</h1>
          <BalancedText text={payload.description} className="topbar-copy-body" maxLines={1} />
        </div>
        <div className="topbar-actions">
          {payload.library_href ? (
            <button type="button" className="action-secondary" onClick={() => { window.location.href = payload.library_href! }}>
              Back to library
            </button>
          ) : null}
          <ThemePicker
            themes={themes}
            themeName={themeName}
            mode={mode}
            onThemeChange={onThemeChange}
            onModeChange={onModeChange}
          />
        </div>
      </header>

      <main className="session-layout">
        <aside className="navigator-rail">
          <div className="rail-head">
            <div className="mono-kicker">Navigator</div>
            <strong>Turns & transport</strong>
          </div>
          <div className="transport-row">
            <button type="button" className="transport-button" onClick={() => { setPlaying(false); setSelectedFrameIndex(0) }}>
              start
            </button>
            <button
              type="button"
              className="transport-button"
              onClick={() => { setPlaying(false); setSelectedFrameIndex((index) => Math.max(0, index - 1)) }}
            >
              prev
            </button>
            <button type="button" className="transport-button strong" onClick={() => setPlaying((value) => !value)}>
              {playing ? 'pause' : 'play'}
            </button>
            <button
              type="button"
              className="transport-button"
              onClick={() => { setPlaying(false); setSelectedFrameIndex((index) => Math.min(payload.frames.length - 1, index + 1)) }}
            >
              next
            </button>
          </div>
          <div className="frame-meta">
            <span>frame {Math.min(payload.frames.length, selectedFrameIndex + 1)} / {payload.frames.length}</span>
            <span>{selectedFrame ? formatDuration(selectedFrame.scenario_elapsed_s) : '0s'}</span>
          </div>
          <div className="turn-list">
            {payload.turn_summaries.map((turn) => {
              const active = selectedTurn === turn.turn_index
              return (
                <button
                  key={turn.turn_index}
                  type="button"
                  className={active ? 'turn-row is-active' : 'turn-row'}
                  onClick={() => jumpToTurn(turn.turn_index)}
                >
                  <strong>turn {turn.turn_index}</strong>
                  <span>{summarizeTurn(turn)}</span>
                </button>
              )
            })}
          </div>
        </aside>

        <section className="workspace">
          <div className="workspace-header">
            <div className="workspace-copy">
              <div className="mono-kicker">Playback</div>
              <strong>Session viewport</strong>
            </div>
            <div className="workspace-stats">
              <span>{payload.stats.turn_count} turns</span>
              <span>{payload.stats.trace_event_count} events</span>
              <span>peak agent {payload.stats.max_agent_turn}</span>
            </div>
          </div>
          <div className="workspace-stage">
            <TerminalArtboard
              frame={selectedFrame}
              turnSummary={selectedTurnSummary}
              selectedEvent={selectedEvent}
            />
          </div>
          <section className="timeline-dock">
            <div className="timeline-dock-head">
              <div className="timeline-dock-copy">
                <div className="mono-kicker">Trace</div>
                <strong>Event browser</strong>
              </div>
              <div className="timeline-dock-tools">
                <input
                  className="trace-filter"
                  value={traceFilter}
                  onChange={(event) => setTraceFilter(event.target.value)}
                  placeholder="Filter trace events…"
                />
                <div className="timeline-dock-meta">
                  <span>{filteredEvents.length} visible</span>
                  <span>{payload.trace_events.length} total</span>
                </div>
              </div>
            </div>
            <div className="timeline-grid">
              {filteredEvents.map((event, index) => {
                const type = String(event.type ?? 'event')
                const active = selectedEvent === event
                return (
                  <button
                    key={`${type}-${index}`}
                    type="button"
                    className={active ? 'timeline-card is-active' : `timeline-card tone-${eventTone(type)}`}
                    onClick={() => selectEvent(event, index)}
                  >
                    <div className="timeline-card-head">
                      <strong>{type.replaceAll('_', ' ')}</strong>
                      <span>{typeof event.t === 'number' ? formatDuration(event.t) : '—'}</span>
                    </div>
                    <span className="timeline-card-copy">{clamp(summarizeEvent(event), 180)}</span>
                  </button>
                )
              })}
            </div>
          </section>
        </section>

        <aside className="inspector-rail">
          <div className="rail-head">
            <div className="mono-kicker">Inspector</div>
            <strong>Evidence & detail</strong>
          </div>

          <div className="inspector-summary">
            <dl className="inspector-stats inspector-stats-compact">
              <div><dt>Frame</dt><dd>{selectedFrame ? selectedFrame.index : 0}</dd></div>
              <div><dt>Turn</dt><dd>{selectedFrame?.turn_index || 'setup'}</dd></div>
              <div><dt>Elapsed</dt><dd>{selectedFrame ? formatDuration(selectedFrame.scenario_elapsed_s) : '0s'}</dd></div>
              <div><dt>Tools</dt><dd>{selectedFrame?.running_tools ?? 0}</dd></div>
            </dl>
            <BalancedText
              text={selectedEvent ? `${eventSelectionMeta} · ${summarizeEvent(selectedEvent)}` : 'No event selected.'}
              className="inspector-copy"
              font={SMALL_FONT}
              lineHeight={19}
              maxLines={4}
            />
          </div>

          <div className="inspect-tabs">
            <button
              type="button"
              className={detailTab === 'focus' ? 'inspect-tab is-active' : 'inspect-tab'}
              onClick={() => setDetailTab('focus')}
            >
              focus
            </button>
            <button
              type="button"
              className={detailTab === 'artifacts' ? 'inspect-tab is-active' : 'inspect-tab'}
              onClick={() => setDetailTab('artifacts')}
            >
              artifacts
            </button>
          </div>

          {detailTab === 'focus' ? (
            <div className="inspect-pane">
              <div className="inspect-pane-head">
                <div className="mono-kicker">Selected event</div>
                <strong className="inspect-pane-title">
                  {String(selectedEvent?.type ?? 'event').replaceAll('_', ' ')}
                </strong>
                <BalancedText
                  text={selectedEvent ? summarizeEvent(selectedEvent) : 'No event selected.'}
                  className="inspect-pane-copy"
                  font={SMALL_FONT}
                  lineHeight={20}
                  maxLines={4}
                />
              </div>
              {imageArtifact ? (
                <div className="inspect-preview">
                  <div className="mono-kicker">Reference still</div>
                  <img src={imageArtifact.href} alt={imageArtifact.name} />
                </div>
              ) : null}
              {selectedEvent ? (
                <div className="inspect-detail">
                  <div className="mono-kicker">Event payload</div>
                  <pre>{selectedEventJson}</pre>
                </div>
              ) : null}
            </div>
          ) : null}

          {detailTab === 'artifacts' ? (
            <div className="inspect-pane">
              <div className="inspect-pane-head">
                <div className="mono-kicker">Artifacts</div>
                <strong className="inspect-pane-title">Bundle outputs</strong>
              </div>
              <dl className="inspector-stats inspector-stats-compact">
                <div><dt>Frames</dt><dd>{payload.stats.frame_count}</dd></div>
                <div><dt>Turns</dt><dd>{payload.stats.turn_count}</dd></div>
                <div><dt>Trace</dt><dd>{payload.stats.trace_event_count}</dd></div>
                <div><dt>Duration</dt><dd>{formatDuration(payload.stats.duration_s)}</dd></div>
              </dl>
              <div className="artifact-list artifact-list-scroll">
                {allArtifacts.map((artifact) => (
                  <a key={`${artifact.kind}-${artifact.href}`} href={artifact.href} className="artifact-row">
                    <strong>{artifact.name}</strong>
                    <span>{artifact.kind}</span>
                  </a>
                ))}
              </div>
            </div>
          ) : null}
        </aside>
      </main>
    </div>
  )
}

function App() {
  const payload = window.__SUCCESSOR_REVIEWER_BOOTSTRAP__
  if (!payload) {
    return <div className="boot-error">Missing reviewer bootstrap payload.</div>
  }

  const { mode, setMode, themeName, setThemeName } = useThemeState(payload)

  if (payload.kind === 'library') {
    return (
      <LibraryBase
        payload={payload}
        themes={payload.theme_catalog}
        themeName={themeName}
        mode={mode}
        onThemeChange={setThemeName}
        onModeChange={setMode}
      />
    )
  }

  return (
    <SessionWorkbench
      payload={payload}
      themes={payload.theme_catalog}
      themeName={themeName}
      mode={mode}
      onThemeChange={setThemeName}
      onModeChange={setMode}
    />
  )
}

export default App
