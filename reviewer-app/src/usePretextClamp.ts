import { layoutWithLines, prepareWithSegments } from '@chenglou/pretext'
import type { RefObject } from 'react'
import { useEffect, useMemo, useRef, useState } from 'react'

function fitLastLine(line: string, width: number, font: string, lineHeight: number): string {
  let candidate = line.trimEnd().replace(/[.,:;!?]+$/, '')
  while (candidate.length > 1) {
    const trial = `${candidate}…`
    const prepared = prepareWithSegments(trial, font)
    const layout = layoutWithLines(prepared, width, lineHeight)
    if (layout.lineCount <= 1) {
      return trial
    }
    candidate = candidate.slice(0, -1).trimEnd()
  }
  return '…'
}

export function clampTextToLines(
  text: string,
  width: number,
  font: string,
  lineHeight: number,
  maxLines: number,
): string {
  if (!text.trim() || width <= 0 || maxLines <= 0) {
    return text
  }
  const prepared = prepareWithSegments(text, font)
  const layout = layoutWithLines(prepared, width, lineHeight)
  if (layout.lineCount <= maxLines) {
    return text
  }
  const visible = layout.lines.slice(0, maxLines).map((line) => line.text)
  visible[maxLines - 1] = fitLastLine(visible[maxLines - 1] ?? '', width, font, lineHeight)
  return visible.join('\n')
}

export function useElementWidth<T extends HTMLElement>(): [RefObject<T | null>, number] {
  const ref = useRef<T | null>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const node = ref.current
    if (!node) {
      return
    }
    const update = () => setWidth(Math.floor(node.clientWidth))
    update()
    const observer = new ResizeObserver(() => update())
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  return [ref, width]
}

export function useClampedText(
  text: string,
  font: string,
  lineHeight: number,
  maxLines: number,
): [RefObject<HTMLDivElement | null>, string] {
  const [ref, width] = useElementWidth<HTMLDivElement>()
  const clamped = useMemo(
    () => clampTextToLines(text, width, font, lineHeight, maxLines),
    [font, lineHeight, maxLines, text, width],
  )
  return [ref, clamped]
}
