import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'

import CaseForm from '@/components/domain/CaseForm.vue'
import IssueList from '@/components/domain/IssueList.vue'
import { usePolling } from '@/composables/usePolling'
import { isWorking, statusTone } from '@/pages/instructionStatus'
import type { CaseContent } from '@/types/api'

const generated: CaseContent = {
  title: 'Bill amount below the minimum',
  description: 'No points under 500.',
  category: 'boundary',
  test_type: 'ui',
  objective_id: 'OBJ-1',
  preconditions: ['A customer is selected'],
  test_data: [{ name: 'bill_amount', value: '499', description: null }],
  steps: [
    {
      action: 'navigate',
      description: 'Open billing',
      target: null,
      value: '/billing',
      side_effect: 'none',
    },
    {
      action: 'fill',
      description: 'Enter the amount',
      target: 'the Bill amount field',
      value: '{{data.bill_amount}}',
      side_effect: 'none',
    },
    {
      action: 'assert_text',
      description: 'Check the points',
      target: 'the Points value',
      value: '0',
      side_effect: 'none',
    },
  ],
  expected_result: 'No points are awarded.',
  expected_source: 'plan_objective',
  priority: 'p1',
  severity: 'major',
  tags: ['loyalty'],
  dependencies: [],
  automation_feasibility: 'automatable',
  automation_notes: null,
  verification: { method: 'ui_assertion', description: 'The points value shows 0.' },
  assumptions: [],
}

function mountForm(content?: CaseContent) {
  const save = vi.fn<(content: CaseContent) => Promise<void>>(async () => {})
  const wrapper = mount(CaseForm, { props: { content, save } })
  const submit = async () => {
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    return save.mock.calls.at(-1)![0]
  }
  return { wrapper, save, submit }
}

describe('CaseForm', () => {
  it('round-trips a generated case unchanged, keeping where its expected result came from', async () => {
    const { submit } = mountForm(generated)
    expect(await submit()).toEqual(generated)
  })

  it('labels an expected result typed by the reviewer as their own', async () => {
    const unknown = { ...generated, expected_result: null, expected_source: 'unknown' as const }
    const { wrapper, submit } = mountForm(unknown)
    const expectedField = wrapper.findAll('textarea').find((t) => t.element.value === '')!
    expect((await submit()).expected_source).toBe('unknown')

    const result = wrapper
      .findAll('textarea')
      .filter((t) => t.element.closest('.field')?.textContent?.includes('Expected result'))[0]!
    expect(result.element).toBe(expectedField.element)
    await result.setValue('An error says the amount must be positive.')
    const saved = await submit()
    expect(saved.expected_result).toBe('An error says the amount must be positive.')
    expect(saved.expected_source).toBe('human_specified')
  })

  it('does not let a changed result keep the plan as its source, and empties become unknown', async () => {
    const { wrapper, submit } = mountForm(generated)
    const result = wrapper
      .findAll('textarea')
      .filter((t) => t.element.closest('.field')?.textContent?.includes('Expected result'))[0]!

    await result.setValue('Five points are awarded.')
    expect((await submit()).expected_source).toBe('human_specified')

    await result.setValue('   ')
    const cleared = await submit()
    expect([cleared.expected_result, cleared.expected_source]).toEqual([null, 'unknown'])
  })

  it('adds, reorders and removes steps', async () => {
    const { wrapper, submit } = mountForm(generated)
    const button = (name: string, index = 0) =>
      wrapper.findAll('button').filter((b) => b.text() === name)[index]!

    await button('Move down').trigger('click') // navigate goes below fill
    expect((await submit()).steps.map((s) => s.action)).toEqual(['fill', 'navigate', 'assert_text'])

    await button('Remove step', 1).trigger('click')
    expect((await submit()).steps.map((s) => s.action)).toEqual(['fill', 'assert_text'])

    await button('Add step').trigger('click')
    const added = (await submit()).steps.at(-1)!
    expect(added).toEqual({
      action: 'click',
      description: '',
      target: null,
      value: null,
      side_effect: 'none',
    })
  })

  it('parses test data, tags and multi-line lists for a hand-written case', async () => {
    const { wrapper, submit } = mountForm()
    await wrapper.find('input.input').setValue('Login page is reachable')
    const area = (label: string) =>
      wrapper
        .findAll('textarea')
        .filter((t) => t.element.closest('.field')?.textContent?.includes(label))[0]!
    await area('Preconditions').setValue('Signed out\n\n  Home page open  ')
    await area('Test data').setValue('amount=499\nnote=\nplaceholder')
    const tags = wrapper
      .findAll('input')
      .filter((i) => i.element.closest('.field')?.textContent?.includes('Tags'))[0]!
    await tags.setValue('smoke, , login')

    const saved = await submit()
    expect(saved.title).toBe('Login page is reachable')
    expect(saved.preconditions).toEqual(['Signed out', 'Home page open'])
    expect(saved.test_data).toEqual([
      { name: 'amount', value: '499', description: null },
      { name: 'note', value: '', description: null },
      { name: 'placeholder', value: null, description: null },
    ])
    expect(saved.tags).toEqual(['smoke', 'login'])
    expect([saved.expected_result, saved.expected_source, saved.objective_id]).toEqual([
      null,
      'unknown',
      null,
    ])
    expect(saved.steps).toHaveLength(1)
  })

  it('shows unconfirmed assumptions only when there are some, and reports save errors', async () => {
    expect(mountForm(generated).wrapper.text()).not.toContain('Unconfirmed assumptions')
    const assumed = mountForm({ ...generated, assumptions: ['Points are rounded down'] })
    expect(assumed.wrapper.text()).toContain('Unconfirmed assumptions')

    const failing = mount(CaseForm, {
      props: {
        content: generated,
        save: async () => {
          throw new Error('boom')
        },
      },
    })
    await failing.find('form').trigger('submit')
    await flushPromises()
    expect(failing.find('[role="alert"]').text()).toContain('Something went wrong')
  })
})

describe('IssueList', () => {
  it('distinguishes blocking issues from notes and names the step', () => {
    const wrapper = mount(IssueList, {
      props: {
        issues: [
          {
            code: 'missing_expected_result',
            severity: 'blocking',
            message: 'The expected result is not known.',
          },
          {
            code: 'vague_step',
            severity: 'blocking',
            message: 'The step does not say what to do.',
            step: 3,
          },
          { code: 'not_in_requirement', severity: 'warning', message: 'Beyond the requirement.' },
        ],
      },
    })
    const items = wrapper.findAll('li')
    expect(items.map((i) => i.classes().includes('issue--blocking'))).toEqual([true, true, false])
    expect(items[1]!.text()).toContain('Step 3: The step does not say what to do.')
    expect(
      mount(IssueList, { props: { issues: [] } })
        .find('ul')
        .exists(),
    ).toBe(false)
  })
})

describe('usePolling', () => {
  it('polls while work is in progress and stops when it finishes or the page closes', async () => {
    vi.useFakeTimers()
    let remaining = 3
    const tick = vi.fn(async () => {
      remaining -= 1
    })
    let controls!: ReturnType<typeof usePolling>
    const Host = defineComponent({
      setup() {
        controls = usePolling(tick, () => remaining > 0, 1000)
        return () => h('div')
      },
    })
    const wrapper = mount(Host)
    controls.start()
    controls.start() // starting twice does not double the requests

    await vi.advanceTimersByTimeAsync(10_000)
    expect(tick).toHaveBeenCalledTimes(3)

    remaining = 5
    controls.start()
    await vi.advanceTimersByTimeAsync(1000)
    expect(tick).toHaveBeenCalledTimes(4)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(tick).toHaveBeenCalledTimes(4)
    vi.useRealTimers()
  })
})

describe('instruction status helpers', () => {
  it('knows which statuses mean background work', () => {
    expect(['analysing', 'generating', 'received'].every((s) => isWorking(s as never))).toBe(true)
    expect(
      ['analysed', 'needs_clarification', 'awaiting_approval', 'failed', 'completed'].some((s) =>
        isWorking(s as never),
      ),
    ).toBe(false)
    expect(statusTone('failed')).toBe('danger')
    expect(statusTone('needs_clarification')).toBe('warning')
    expect(statusTone('completed')).toBe('success')
  })
})
