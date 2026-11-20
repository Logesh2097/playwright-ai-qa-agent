<script setup lang="ts">
import { ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import ToastHost from '@/components/ui/ToastHost.vue'
import { env } from '@/config/env'
import { navigation } from '@/router/navigation'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const menuOpen = ref(false)

async function switchOrganization(event: Event): Promise<void> {
  auth.selectOrganization((event.target as HTMLSelectElement).value)
  // Whatever was open belonged to the previous organization.
  await router.push({ name: 'home' })
}

async function signOut(): Promise<void> {
  await auth.logout()
  await router.push({ name: 'login' })
}

// On small screens the sidebar is a drawer; close it after navigating.
watch(
  () => route.fullPath,
  () => {
    menuOpen.value = false
  },
)
</script>

<template>
  <div class="shell">
    <a class="skip-link" href="#main">Skip to content</a>

    <header class="shell__header">
      <button
        class="shell__menu-button"
        type="button"
        :aria-expanded="menuOpen"
        aria-controls="primary-navigation"
        @click="menuOpen = !menuOpen"
      >
        <span aria-hidden="true">☰</span>
        <span class="visually-hidden">Toggle navigation</span>
      </button>
      <RouterLink class="shell__brand" :to="{ name: 'home' }">{{ env.appName }}</RouterLink>

      <div class="shell__account">
        <label v-if="auth.memberships.length > 1" class="shell__org">
          <span class="visually-hidden">Organization</span>
          <select class="input" :value="auth.organizationId ?? ''" @change="switchOrganization">
            <option
              v-for="item in auth.memberships"
              :key="item.organization_id"
              :value="item.organization_id"
            >
              {{ item.organization_name }}
            </option>
          </select>
        </label>
        <span v-else-if="auth.membership" class="shell__org-name">
          {{ auth.membership.organization_name }}
        </span>
        <span v-if="auth.role" class="tag">{{ auth.role.replace('_', ' ') }}</span>
        <button class="shell__signout" type="button" @click="signOut">Sign out</button>
      </div>
    </header>

    <div v-if="menuOpen" class="shell__backdrop" @click="menuOpen = false" />

    <nav
      id="primary-navigation"
      class="shell__sidebar"
      :class="{ 'shell__sidebar--open': menuOpen }"
      aria-label="Primary"
    >
      <RouterLink
        v-for="item in navigation"
        :key="item.name"
        class="shell__nav-link"
        :to="{ name: item.name }"
      >
        {{ item.label }}
      </RouterLink>
    </nav>

    <main id="main" class="shell__main" tabindex="-1">
      <!-- Keyed by organization: switching it discards every page's loaded data. -->
      <RouterView :key="auth.organizationId ?? 'none'" />
    </main>

    <ToastHost />
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-areas:
    'header header'
    'sidebar main';
  grid-template-rows: var(--header-height) 1fr;
  grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
  min-height: 100dvh;
}

.skip-link {
  position: absolute;
  top: var(--space-2);
  left: var(--space-2);
  z-index: 200;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--color-surface);
  transform: translateY(-200%);
}

.skip-link:focus {
  transform: none;
}

.shell__header {
  grid-area: header;
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 0 var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
}

.shell__brand {
  color: var(--color-text);
  font-weight: 700;
  letter-spacing: -0.01em;
  text-decoration: none;
}

.shell__account {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-left: auto;
  min-width: 0;
}

.shell__org .input {
  min-height: 2rem;
  max-width: 14rem;
}

.shell__org-name {
  overflow: hidden;
  font-size: var(--text-sm);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.shell__signout {
  flex: none;
  padding: var(--space-1) var(--space-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  cursor: pointer;
}

.shell__signout:hover {
  background: var(--color-surface-muted);
  color: var(--color-text);
}

.shell__menu-button {
  display: none;
  width: 2.5rem;
  height: 2.5rem;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text);
  font-size: var(--text-lg);
  cursor: pointer;
}

.shell__sidebar {
  grid-area: sidebar;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-4) var(--space-3);
  border-right: 1px solid var(--color-border);
  background: var(--color-surface);
}

.shell__nav-link {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  font-weight: 600;
  text-decoration: none;
}

.shell__nav-link:hover {
  background: var(--color-surface-muted);
  color: var(--color-text);
}

.shell__nav-link.router-link-exact-active {
  background: var(--color-surface-muted);
  color: var(--color-accent);
}

.shell__main {
  grid-area: main;
  width: 100%;
  max-width: 64rem;
  padding: var(--space-6) var(--space-5);
  outline: none;
}

.shell__backdrop {
  display: none;
}

@media (max-width: 768px) {
  .shell {
    grid-template-areas:
      'header'
      'main';
    grid-template-columns: minmax(0, 1fr);
  }

  .shell__menu-button {
    display: inline-grid;
    place-items: center;
  }

  .shell__sidebar {
    position: fixed;
    top: var(--header-height);
    bottom: 0;
    left: 0;
    z-index: 25;
    width: min(var(--sidebar-width), 80vw);
    transform: translateX(-100%);
    transition: transform 160ms ease;
    visibility: hidden;
  }

  .shell__sidebar--open {
    transform: none;
    visibility: visible;
    box-shadow: var(--shadow-md);
  }

  .shell__backdrop {
    display: block;
    position: fixed;
    inset: var(--header-height) 0 0 0;
    z-index: 20;
    background: var(--color-overlay);
  }

  .shell__main {
    padding: var(--space-5) var(--space-4);
  }

  .shell__account .tag,
  .shell__org-name {
    display: none;
  }
}
</style>
