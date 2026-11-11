// One date format for the whole interface: "Nov 10, 2026" and "Nov 10, 2026, 8:07 PM".
const DATE: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', year: 'numeric' }
const TIME: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit' }

function parse(value: string | Date | null | undefined): Date | null {
  if (!value) return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatDate(value: string | Date | null | undefined): string | null {
  const date = parse(value)
  return date ? date.toLocaleDateString(undefined, DATE) : null
}

export function formatDateTime(value: string | Date | null | undefined): string | null {
  const date = parse(value)
  return date ? date.toLocaleString(undefined, { ...DATE, ...TIME }) : null
}

export function formatTime(value: string | Date | null | undefined): string | null {
  const date = parse(value)
  return date ? date.toLocaleTimeString(undefined, TIME) : null
}
