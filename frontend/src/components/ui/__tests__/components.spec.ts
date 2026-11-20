import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import { useAsync } from '@/composables/useAsync'

describe('BaseButton', () => {
  it('renders its slot and is a plain button by default', () => {
    const wrapper = mount(BaseButton, { slots: { default: 'Save' } })
    expect(wrapper.text()).toBe('Save')
    expect(wrapper.attributes('type')).toBe('button')
    expect(wrapper.attributes('disabled')).toBeUndefined()
  })

  it('is disabled and marked busy while loading', () => {
    const wrapper = mount(BaseButton, { props: { loading: true } })
    expect(wrapper.attributes('disabled')).toBeDefined()
    expect(wrapper.attributes('aria-busy')).toBe('true')
  })
})

describe('LoadingState', () => {
  it('announces its label', () => {
    const wrapper = mount(LoadingState, { props: { label: 'Checking services…' } })
    expect(wrapper.attributes('role')).toBe('status')
    expect(wrapper.text()).toContain('Checking services…')
  })
})

describe('ErrorState', () => {
  it('shows the message as an alert without a retry button by default', () => {
    const wrapper = mount(ErrorState, { props: { message: 'Could not reach the server.' } })
    expect(wrapper.attributes('role')).toBe('alert')
    expect(wrapper.text()).toContain('Could not reach the server.')
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('emits retry when retryable', async () => {
    const wrapper = mount(ErrorState, { props: { message: 'Failed', retryable: true } })
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('renders markup in messages as text', () => {
    const wrapper = mount(ErrorState, { props: { message: '<img src=x onerror=alert(1)>' } })
    expect(wrapper.find('img').exists()).toBe(false)
  })
})

describe('useAsync', () => {
  it('tracks loading, data and error', async () => {
    const ok = useAsync(async (value: number) => value * 2)
    const pending = ok.run(21)
    expect(ok.loading.value).toBe(true)
    await pending
    expect(ok.data.value).toBe(42)
    expect(ok.loading.value).toBe(false)

    const failing = useAsync(async () => {
      throw new Error('internal detail')
    })
    await expect(failing.run()).resolves.toBeNull()
    expect(failing.error.value).toBe('Something went wrong. Please try again.')
  })

  it('keeps only the latest result when calls overlap', async () => {
    const resolvers: Array<(value: string) => void> = []
    const state = useAsync(() => new Promise<string>((resolve) => resolvers.push(resolve)))

    const first = state.run()
    const second = state.run()
    resolvers[1]!('second')
    await second
    resolvers[0]!('first')
    await first

    expect(state.data.value).toBe('second')
  })
})
