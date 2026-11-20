import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { reportsApi } from '@/api/reports'
import TrendChart from '@/components/domain/TrendChart.vue'
import DashboardPage from '@/pages/DashboardPage.vue'
import ExecutionReportPage from '@/pages/ExecutionReportPage.vue'
import { duration, label, percent, showValue, toneOf } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import type {
  Dashboard,
  ExecutionReport,
  ResultDetail,
  ResultRates,
  TrendPoint,
} from '@/types/reports'

const global = { stubs: { RouterLink: RouterLinkStub } }

function rates(passed: number, failed: number): ResultRates {
  const ran = passed + failed
  return {
    ran,
    passed,
    failed,
    error: 0,
    flaky: 0,
    needs_review: 0,
    skipped: 0,
    blocked: 0,
    pass_rate: ran ? passed / ran : null,
    failure_rate: ran ? failed / ran : null,
  }
}

function point(date: string, passed: number, failed: number): TrendPoint {
  return {
    date,
    executions: passed + failed ? 1 : 0,
    verdicts: {},
    results: rates(passed, failed),
    average_duration_ms: null,
  }
}

function result(overrides: Partial<ResultDetail>): ResultDetail {
  return {
    id: 'r1',
    case_id: 'c1',
    case_key: 'TC-0001',
    title: 'Loyalty points',
    version: 1,
    expected_result: '50 points are shown',
    status: 'passed',
    failure_category: null,
    failure_summary: null,
    healed: false,
    attempts: 1,
    started_at: '2026-11-19T10:00:00Z',
    finished_at: '2026-11-19T10:00:02Z',
    duration_ms: 2000,
    console_errors: [],
    page_errors: [],
    network_errors: [],
    verification: null,
    steps: [],
    evidence: [],
    ai_suggestion: null,
    ...overrides,
  }
}

const failed = result({
  id: 'r2',
  case_key: 'TC-0002',
  title: '<img src=x onerror=alert(1)>',
  status: 'failed',
  failure_category: 'test_assertion_failure',
  failure_summary: 'expected 50, found 51',
  steps: [
    {
      attempt: 1,
      position: 1,
      phase: 'main',
      action: 'assert_text',
      description: 'Check the points',
      status: 'failed',
      started_at: null,
      finished_at: null,
      duration_ms: 120,
      locator: null,
      healed_from: null,
      expected: { equals: '50' },
      actual: { text: '51' },
      error_type: 'assertion',
      error_message: 'expected 50, found 51',
      page_url: null,
    },
  ],
  evidence: [
    {
      id: 'e1',
      kind: 'screenshot',
      content_type: 'image/png',
      size_bytes: 2048,
      step_position: 1,
      expires_at: null,
    },
  ],
  ai_suggestion: {
    summary: 'The page showed 51.',
    possible_root_cause: 'Possibly a rounding difference.',
    impact: 'Wrong points.',
    debugging_steps: ['Check the rule.'],
    note: 'AI-generated suggestion. Not a confirmed root cause, and it does not affect any status.',
    advisory: true,
  },
})

const report: ExecutionReport = {
  report_version: 1,
  generated_at: '2026-11-19T10:05:00Z',
  project: { id: 'p1', name: 'Web shop' },
  application: { id: 'a1', name: 'Storefront' },
  environment: {
    id: 'env1',
    name: 'staging',
    kind: 'staging',
    base_url: 'https://staging.acme.io',
  },
  instruction: { id: 'i1', text: 'Check that loyalty points follow the rule.' },
  suite: { id: 's1', name: 'Loyalty' },
  execution: {
    id: '11111111-2222-3333-4444-555555555555',
    status: 'completed',
    verdict: 'failed',
    started_at: '2026-11-19T10:00:00Z',
    finished_at: '2026-11-19T10:01:15Z',
    duration_ms: 75000,
    triggered_by: 'Ada Lovelace',
    browser: 'chromium',
    config: {},
    error: null,
  },
  totals: {
    total: 2,
    passed: 1,
    failed: 1,
    skipped: 0,
    blocked: 0,
    error: 0,
    flaky: 0,
    needs_review: 0,
    pass_rate: 0.5,
  },
  failure_categories: { test_assertion_failure: 1 },
  failures: [
    {
      result_id: 'r2',
      case_key: 'TC-0002',
      title: failed.title,
      status: 'failed',
      failure_category: 'test_assertion_failure',
      summary: 'expected 50, found 51',
      failed_step: 'Step 1: Check the points',
      expected: { equals: '50' },
      actual: { text: '51' },
      screenshot_ids: ['e1'],
    },
  ],
  results: [result({}), failed],
  notes: ['This report describes test results. It does not measure code coverage.'],
}

beforeEach(() => {
  setActivePinia(createPinia())
  useAuthStore().organizationId = 'org1'
  vi.restoreAllMocks()
})

describe('report formatting', () => {
  it('shows only a pass as a success', () => {
    expect(toneOf('passed')).toBe('success')
    for (const status of ['flaky', 'needs_review', 'inconclusive']) {
      expect(toneOf(status)).toBe('warning')
    }
    expect(toneOf('failed')).toBe('danger')
    expect(toneOf('skipped')).toBe('neutral')
    expect(toneOf(null)).toBe('neutral')
  })

  it('shows a rate with nothing behind it as a dash, not as 0% or 100%', () => {
    expect(percent(null)).toBe('—')
    expect(percent(0)).toBe('0%')
    expect(percent(1)).toBe('100%')
    expect(percent(0.3333)).toBe('33.3%')
  })

  it('formats durations, labels and recorded values', () => {
    expect([duration(null), duration(120), duration(2000), duration(75000)]).toEqual([
      '—',
      '120 ms',
      '2.0 s',
      '1 min 15 s',
    ])
    expect(label('test_assertion_failure')).toBe('test assertion failure')
    expect(showValue({ equals: '50' })).toBe('{"equals":"50"}')
    expect(showValue(null)).toBe('—')
  })
})

describe('TrendChart', () => {
  const points = [point('2026-11-17', 3, 1), point('2026-11-18', 0, 0), point('2026-11-19', 4, 0)]

  it('draws a point only for days on which something ran, and labels the last value', () => {
    const wrapper = mount(TrendChart, { props: { points } })
    expect(wrapper.findAll('circle')).toHaveLength(2)
    // A day without results breaks the line: it is not drawn as 0%.
    expect(wrapper.findAll('path.trend__line')).toHaveLength(0)
    expect(wrapper.find('.trend__end').text()).toBe('100%')
    expect(wrapper.find('svg').attributes('aria-label')).toContain('3 days')
  })

  it('offers every figure as a table and by keyboard', async () => {
    const wrapper = mount(TrendChart, { props: { points } })
    await wrapper.find('svg').trigger('keydown', { key: 'ArrowLeft' })
    expect(wrapper.find('.trend__readout').text()).toBe('Nothing ran to an end on 2026-11-18.')
    await wrapper.find('svg').trigger('keydown', { key: 'ArrowLeft' })
    expect(wrapper.find('.trend__readout').text()).toContain('75.0%')
    expect(wrapper.find('.trend__readout').text()).toContain('3 of 4 cases passed')

    await wrapper.find('button').trigger('click')
    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(3)
    expect(rows[1]!.text()).toContain('—')
    expect(rows[0]!.text()).toContain('75.0%')
  })

  it('says so when nothing ran, and draws no chart', () => {
    const wrapper = mount(TrendChart, { props: { points: [point('2026-11-19', 0, 0)] } })
    expect(wrapper.find('svg').exists()).toBe(false)
    expect(wrapper.text()).toContain('No test case ran')
  })
})

describe('ExecutionReportPage', () => {
  async function open() {
    vi.spyOn(reportsApi, 'report').mockResolvedValue(report)
    const wrapper = mount(ExecutionReportPage, {
      props: { projectId: 'p1', executionId: report.execution.id },
      global,
    })
    await flushPromises()
    return wrapper
  }

  it('shows what was tested, the totals and the final status from the report', async () => {
    const wrapper = await open()
    const text = wrapper.text()
    for (const part of [
      'Web shop',
      'Storefront',
      'staging',
      'Loyalty',
      report.execution.id,
      '1 min 15 s',
      'Ada Lovelace',
      'Check that loyalty points follow the rule.',
      'It does not measure code coverage.',
    ]) {
      expect(text).toContain(part)
    }
    const tiles = wrapper.findAll('.tile').map((tile) => tile.text())
    expect(tiles).toContain('2Total')
    expect(tiles).toContain('1Passed')
    expect(tiles).toContain('1Failed')
    expect(tiles).toContain('50.0%Pass rate of cases run')
    expect(wrapper.find('h1 + .badge, .cluster .badge').text()).toBe('failed')
  })

  it('opens failed cases with their steps and shows the AI text as a labelled suggestion', async () => {
    const wrapper = await open()
    const cards = wrapper.findAll('.result')
    expect(cards[0]!.find('table').exists()).toBe(false) // the passing case stays closed
    const bad = cards[1]!
    expect(bad.find('table').text()).toContain('{"equals":"50"}')
    expect(bad.find('table').text()).toContain('{"text":"51"}')
    expect(bad.text()).toContain('test assertion failure')
    const suggestion = bad.find('.suggestion')
    expect(suggestion.text()).toContain('AI-generated suggestion')
    expect(suggestion.text()).toContain('not a confirmed root cause')
    expect(suggestion.text()).toContain('Possibly a rounding difference.')
  })

  it('never renders text from the application as markup', async () => {
    const wrapper = await open()
    expect(wrapper.find('img[src="x"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>')
  })

  it('fetches a screenshot with the session and only when asked', async () => {
    const evidence = vi
      .spyOn(reportsApi, 'evidence')
      .mockResolvedValue({ blob: new Blob(['png']), filename: 'screenshot-e1.png' })
    URL.createObjectURL = vi.fn(() => 'blob:shot')
    URL.revokeObjectURL = vi.fn()
    const wrapper = await open()
    expect(evidence).not.toHaveBeenCalled()
    expect(wrapper.find('img.shot__thumb').exists()).toBe(false)

    const show = wrapper.findAll('button').find((button) => button.text() === 'Show')!
    await show.trigger('click')
    await flushPromises()

    expect(evidence).toHaveBeenCalledWith('org1', 'p1', report.execution.id, 'e1')
    expect(wrapper.find('img.shot__thumb').attributes('src')).toBe('blob:shot')
    wrapper.unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:shot')
  })

  it('exports in the chosen format', async () => {
    const exported = vi
      .spyOn(reportsApi, 'exportReport')
      .mockResolvedValue({ blob: new Blob(['%PDF']), filename: 'test-report.pdf' })
    URL.createObjectURL = vi.fn(() => 'blob:file')
    URL.revokeObjectURL = vi.fn()
    const wrapper = await open()
    const button = wrapper.findAll('button').find((item) => item.text() === 'Export PDF')!
    await button.trigger('click')
    await flushPromises()
    expect(exported).toHaveBeenCalledWith('org1', 'p1', report.execution.id, 'pdf')
  })
})

describe('DashboardPage', () => {
  const dashboard: Dashboard = {
    window_days: 30,
    truncated: false,
    total_executions: 5,
    finished_executions: 5,
    executions_by_verdict: { passed: 1, failed: 4 },
    execution_pass_rate: 0.2,
    execution_failure_rate: 0.8,
    results: { ...rates(7, 6), pass_rate: 0.5385, failure_rate: 0.4615 },
    flaky_test_count: 1,
    flaky_case_ids: ['c3'],
    average_execution_duration_ms: 61000,
    frequently_failing: [
      {
        case_id: 'c2',
        case_key: 'TC-0002',
        title: 'Broken',
        runs: 4,
        failures: 4,
        failure_rate: 1,
        last_status: 'failed',
        last_failed_at: null,
        most_common_category: 'test_assertion_failure',
      },
    ],
    coverage: {
      test_cases: 5,
      approved: 4,
      automated: 3,
      executed: 3,
      passing: 1,
      approved_share: 0.8,
      automated_share: 0.75,
      executed_share: 1,
      passing_share: 0.3333,
      note: 'They are not code coverage: no code instrumentation is integrated.',
    },
    recent_executions: [],
  }

  it('shows the key figures and says that coverage is not code coverage', async () => {
    vi.spyOn(reportsApi, 'dashboard').mockResolvedValue(dashboard)
    vi.spyOn(reportsApi, 'trends').mockResolvedValue({
      window_days: 30,
      truncated: false,
      points: [point('2026-11-19', 7, 6)],
    })
    const wrapper = mount(DashboardPage, { props: { projectId: 'p1' }, global })
    await flushPromises()

    const tiles = wrapper.findAll('.tile').map((tile) => tile.text().replace(/\s+/g, ' '))
    expect(tiles).toContain('5Executions')
    expect(tiles.some((tile) => /^53\.[89]% ?Pass rate/.test(tile))).toBe(true)
    expect(tiles.some((tile) => /^46\.[12]% ?Failure rate/.test(tile))).toBe(true)
    expect(tiles).toContain('1Flaky test cases')
    expect(tiles).toContain('1 min 1 sAverage execution time')
    expect(wrapper.text()).toContain('Test coverage indicators')
    expect(wrapper.text()).toContain('not code coverage')
    expect(wrapper.text()).toContain('TC-0002')
  })

  it('shows an empty state, not zero percent, when nothing has run', async () => {
    vi.spyOn(reportsApi, 'dashboard').mockResolvedValue({
      ...dashboard,
      total_executions: 0,
      results: rates(0, 0),
    })
    vi.spyOn(reportsApi, 'trends').mockResolvedValue({
      window_days: 30,
      truncated: false,
      points: [],
    })
    const wrapper = mount(DashboardPage, { props: { projectId: 'p1' }, global })
    await flushPromises()
    expect(wrapper.text()).toContain('No executions in this period')
    expect(wrapper.findAll('.tile')).toHaveLength(0)
  })
})
