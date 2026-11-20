<script setup lang="ts">
/**
 * Pass rate per day, as a line. One series, so the title names it and there is no legend.
 * Days on which nothing ran have no point and the line is broken there: a gap is not 0%.
 * Every value is also in the table view, and in the readout when a day is hovered or focused.
 */
import { computed, ref } from 'vue'

import { percent } from '@/pages/reportFormat'
import type { TrendPoint } from '@/types/reports'

const props = defineProps<{ points: TrendPoint[] }>()

const WIDTH = 640
const HEIGHT = 200
const PAD = { top: 16, right: 48, bottom: 26, left: 40 }
const plotWidth = WIDTH - PAD.left - PAD.right
const plotHeight = HEIGHT - PAD.top - PAD.bottom

const asTable = ref(false)
const active = ref<number | null>(null)

const x = (index: number) =>
  PAD.left + (props.points.length > 1 ? (index / (props.points.length - 1)) * plotWidth : 0)
const y = (rate: number) => PAD.top + (1 - rate) * plotHeight

const marks = computed(() =>
  props.points.flatMap((point, index) =>
    point.results.pass_rate === null
      ? []
      : [{ index, cx: x(index), cy: y(point.results.pass_rate), rate: point.results.pass_rate }],
  ),
)

/** One path per unbroken run of days that have a rate. */
const segments = computed(() => {
  const paths: string[] = []
  let current: string[] = []
  props.points.forEach((point, index) => {
    const rate = point.results.pass_rate
    if (rate === null) {
      if (current.length > 1) paths.push(current.join(' '))
      current = []
      return
    }
    current.push(`${current.length ? 'L' : 'M'}${x(index).toFixed(1)},${y(rate).toFixed(1)}`)
  })
  if (current.length > 1) paths.push(current.join(' '))
  return paths
})

const last = computed(() => marks.value[marks.value.length - 1] ?? null)
const hasData = computed(() => marks.value.length > 0)
const xLabels = computed(() => {
  const count = props.points.length
  if (count === 0) return []
  const picks = [...new Set([0, Math.floor((count - 1) / 2), count - 1])]
  return picks.map((index) => ({ index, text: shortDate(props.points[index]!.date) }))
})
const shown = computed(() => (active.value === null ? null : props.points[active.value]!))

function shortDate(iso: string): string {
  const parsed = new Date(`${iso}T00:00:00Z`)
  return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

function track(event: PointerEvent): void {
  const box = (event.currentTarget as SVGElement).getBoundingClientRect()
  if (box.width === 0 || props.points.length === 0) return
  const inPlot = ((event.clientX - box.left) / box.width) * WIDTH - PAD.left
  const index = Math.round((inPlot / plotWidth) * (props.points.length - 1))
  active.value = Math.min(props.points.length - 1, Math.max(0, index))
}

function step(delta: number): void {
  if (props.points.length === 0) return
  const next = (active.value ?? props.points.length - 1) + delta
  active.value = Math.min(props.points.length - 1, Math.max(0, next))
}
</script>

<template>
  <figure class="trend stack">
    <figcaption class="cluster cluster--between">
      <span>
        <strong>Pass rate per day</strong>
        <span class="small muted"> · share of cases that ran and passed first time</span>
      </span>
      <button type="button" class="trend__toggle small" @click="asTable = !asTable">
        {{ asTable ? 'Show as chart' : 'Show as table' }}
      </button>
    </figcaption>

    <p v-if="!hasData" class="muted small">No test case ran to an end in this period.</p>

    <div v-else-if="asTable" class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>Day</th>
            <th class="num">Executions</th>
            <th class="num">Cases run</th>
            <th class="num">Passed</th>
            <th class="num">Failed or error</th>
            <th class="num">Pass rate</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="point in points" :key="point.date">
            <td>{{ point.date }}</td>
            <td class="num">{{ point.executions }}</td>
            <td class="num">{{ point.results.ran }}</td>
            <td class="num">{{ point.results.passed }}</td>
            <td class="num">{{ point.results.failed + point.results.error }}</td>
            <td class="num">{{ percent(point.results.pass_rate) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <template v-else>
      <svg
        class="trend__plot"
        :viewBox="`0 0 ${WIDTH} ${HEIGHT}`"
        role="img"
        tabindex="0"
        :aria-label="`Pass rate per day over ${points.length} days. Use the arrow keys to read each day, or show the table.`"
        @pointermove="track"
        @pointerleave="active = null"
        @keydown.left.prevent="step(-1)"
        @keydown.right.prevent="step(1)"
        @blur="active = null"
      >
        <g class="trend__grid">
          <template v-for="tick in [0, 0.5, 1]" :key="tick">
            <line :x1="PAD.left" :x2="WIDTH - PAD.right" :y1="y(tick)" :y2="y(tick)" />
            <text :x="PAD.left - 8" :y="y(tick) + 4" text-anchor="end">{{ tick * 100 }}%</text>
          </template>
          <text
            v-for="item in xLabels"
            :key="item.index"
            :x="x(item.index)"
            :y="HEIGHT - 6"
            text-anchor="middle"
          >
            {{ item.text }}
          </text>
        </g>
        <line
          v-if="active !== null"
          class="trend__crosshair"
          :x1="x(active)"
          :x2="x(active)"
          :y1="PAD.top"
          :y2="PAD.top + plotHeight"
        />
        <path v-for="(d, index) in segments" :key="index" class="trend__line" :d="d" />
        <circle
          v-for="mark in marks"
          :key="mark.index"
          class="trend__dot"
          :class="{ 'trend__dot--active': mark.index === active }"
          :cx="mark.cx"
          :cy="mark.cy"
          :r="mark.index === active ? 5 : 4"
        />
        <text v-if="last" class="trend__end" :x="last.cx + 9" :y="last.cy + 4">
          {{ percent(last.rate) }}
        </text>
      </svg>
      <p class="trend__readout small" aria-live="polite">
        <span v-if="shown && shown.results.ran === 0" class="muted">
          Nothing ran to an end on {{ shown.date }}.
        </span>
        <template v-else-if="shown">
          <strong>{{ percent(shown.results.pass_rate) }}</strong>
          <span class="muted">
            on {{ shown.date }} · {{ shown.results.passed }} of {{ shown.results.ran }} cases passed
            · {{ shown.executions }} execution{{ shown.executions === 1 ? '' : 's' }}
          </span>
        </template>
        <span v-else class="muted">Point at a day to read its figures.</span>
      </p>
    </template>
  </figure>
</template>

<style scoped>
.trend {
  margin: 0;
}

.trend__plot {
  width: 100%;
  height: auto;
  max-height: 16rem;
  touch-action: pan-y;
}

.trend__plot:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 2px;
}

.trend__grid line {
  stroke: var(--color-border);
  stroke-width: 1;
}

.trend__grid text,
.trend__end {
  fill: var(--color-text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.trend__end {
  fill: var(--color-text);
  font-weight: 600;
}

.trend__line {
  fill: none;
  stroke: var(--color-chart);
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.trend__dot {
  fill: var(--color-chart);
  stroke: var(--color-surface);
  stroke-width: 2;
}

.trend__crosshair {
  stroke: var(--color-text-muted);
  stroke-width: 1;
}

.trend__toggle {
  padding: 0;
  border: 0;
  background: none;
  color: var(--color-accent);
  cursor: pointer;
  text-decoration: underline;
}

.trend__readout {
  min-height: 1.4em;
  margin: 0;
}
</style>
