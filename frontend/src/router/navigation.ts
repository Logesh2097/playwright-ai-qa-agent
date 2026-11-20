/** Entries of the primary navigation. Later phases append their pages here. */
export interface NavigationItem {
  name: string
  label: string
}

export const navigation: NavigationItem[] = [
  { name: 'home', label: 'Dashboard' },
  { name: 'projects', label: 'Projects' },
  { name: 'integrations', label: 'Integrations' },
  { name: 'settings', label: 'Settings' },
  { name: 'overview', label: 'System status' },
]
