import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { env } from '@/config/env'
import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    /** Reachable without signing in. Everything else requires a session. */
    public?: boolean
  }
}

export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: () => import('@/layouts/AuthLayout.vue'),
    meta: { public: true },
    children: [
      {
        path: 'login',
        name: 'login',
        component: () => import('@/pages/LoginPage.vue'),
        meta: { title: 'Sign in', public: true },
      },
      {
        path: 'register',
        name: 'register',
        component: () => import('@/pages/RegisterPage.vue'),
        meta: { title: 'Create account', public: true },
      },
      {
        path: 'forgot-password',
        name: 'forgot-password',
        component: () => import('@/pages/ForgotPasswordPage.vue'),
        meta: { title: 'Reset password', public: true },
      },
      {
        // Password reset emails link here.
        path: 'reset-password',
        name: 'reset-password',
        component: () => import('@/pages/ResetPasswordPage.vue'),
        meta: { title: 'New password', public: true },
      },
      {
        // Invitation emails link here.
        path: 'accept-invite',
        redirect: (to) => ({ name: 'register', query: to.query }),
      },
    ],
  },
  {
    path: '/',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: '',
        name: 'home',
        component: () => import('@/pages/HomePage.vue'),
        meta: { title: 'Dashboard' },
      },
      {
        path: 'projects',
        name: 'projects',
        component: () => import('@/pages/ProjectsPage.vue'),
        meta: { title: 'Projects' },
      },
      {
        path: 'projects/:projectId',
        name: 'project',
        component: () => import('@/pages/ProjectDetailPage.vue'),
        props: true,
        meta: { title: 'Project' },
      },
      {
        path: 'projects/:projectId/applications/:applicationId',
        name: 'application',
        component: () => import('@/pages/ApplicationPage.vue'),
        props: true,
        meta: { title: 'Application' },
      },
      {
        path: 'projects/:projectId/instructions',
        name: 'instructions',
        component: () => import('@/pages/InstructionsPage.vue'),
        props: true,
        meta: { title: 'Test instructions' },
      },
      {
        path: 'projects/:projectId/instructions/:instructionId',
        name: 'instruction',
        component: () => import('@/pages/InstructionPage.vue'),
        props: true,
        meta: { title: 'Instruction' },
      },
      {
        path: 'projects/:projectId/cases/new',
        name: 'case-new',
        component: () => import('@/pages/TestCasePage.vue'),
        props: true,
        meta: { title: 'New test case' },
      },
      {
        path: 'projects/:projectId/cases/:caseId',
        name: 'case',
        component: () => import('@/pages/TestCasePage.vue'),
        props: true,
        meta: { title: 'Test case' },
      },
      {
        path: 'projects/:projectId/suites',
        name: 'suites',
        component: () => import('@/pages/SuitesPage.vue'),
        props: true,
        meta: { title: 'Test suites' },
      },
      {
        path: 'projects/:projectId/suites/:suiteId',
        name: 'suite',
        component: () => import('@/pages/SuitePage.vue'),
        props: true,
        meta: { title: 'Test suite' },
      },
      {
        path: 'projects/:projectId/dashboard',
        name: 'dashboard',
        component: () => import('@/pages/DashboardPage.vue'),
        props: true,
        meta: { title: 'Test dashboard' },
      },
      {
        path: 'projects/:projectId/executions',
        name: 'executions',
        component: () => import('@/pages/ExecutionsPage.vue'),
        props: true,
        meta: { title: 'Execution history' },
      },
      {
        path: 'projects/:projectId/executions/compare',
        name: 'execution-comparison',
        component: () => import('@/pages/ComparisonPage.vue'),
        props: (route) => ({
          projectId: route.params.projectId,
          base: typeof route.query.base === 'string' ? route.query.base : undefined,
          target: typeof route.query.target === 'string' ? route.query.target : undefined,
        }),
        meta: { title: 'Compare executions' },
      },
      {
        path: 'projects/:projectId/executions/:executionId/live',
        name: 'execution-live',
        component: () => import('@/pages/ExecutionLivePage.vue'),
        props: true,
        meta: { title: 'Execution in progress' },
      },
      {
        path: 'projects/:projectId/heal-proposals',
        name: 'heal-proposals',
        component: () => import('@/pages/HealProposalsPage.vue'),
        props: true,
        meta: { title: 'Replacement locators' },
      },
      {
        path: 'projects/:projectId/integrations',
        name: 'project-integrations',
        component: () => import('@/pages/ProjectIntegrationsPage.vue'),
        props: true,
        meta: { title: 'GitHub repositories' },
      },
      {
        path: 'integrations',
        name: 'integrations',
        component: () => import('@/pages/IntegrationsPage.vue'),
        meta: { title: 'Integrations' },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/pages/SettingsPage.vue'),
        meta: { title: 'Settings' },
      },
      {
        path: 'projects/:projectId/executions/:executionId',
        name: 'execution',
        component: () => import('@/pages/ExecutionReportPage.vue'),
        props: true,
        meta: { title: 'Test report' },
      },
      {
        path: 'projects/:projectId/cases/:caseId/history',
        name: 'case-history',
        component: () => import('@/pages/CaseHistoryPage.vue'),
        props: true,
        meta: { title: 'Result history' },
      },
      {
        path: 'system',
        name: 'overview',
        component: () => import('@/pages/OverviewPage.vue'),
        meta: { title: 'System status' },
      },
      {
        path: ':pathMatch(.*)*',
        name: 'not-found',
        component: () => import('@/pages/NotFoundPage.vue'),
        meta: { title: 'Page not found' },
      },
    ],
  },
]

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory(import.meta.env.BASE_URL),
    routes,
    scrollBehavior: () => ({ top: 0 }),
  })

  // Convenience only: it keeps signed-out visitors off pages that would be empty.
  // The API checks the session and permissions on every request regardless.
  router.beforeEach(async (to) => {
    const auth = useAuthStore()
    await auth.bootstrap()
    if (to.meta.public) {
      return auth.isAuthenticated && to.name !== 'reset-password' ? { name: 'home' } : true
    }
    if (!auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
    return true
  })

  router.afterEach((to) => {
    document.title = to.meta.title ? `${to.meta.title} · ${env.appName}` : env.appName
  })

  return router
}
