export type IncidentFilter = "active" | "waiting" | "resolved"

export function readIncidentListState(searchParams: URLSearchParams) {
  const requestedFilter = searchParams.get("status")
  const filter: IncidentFilter =
    requestedFilter === "waiting" || requestedFilter === "resolved"
      ? requestedFilter
      : "active"

  return { filter, query: searchParams.get("q") ?? "" }
}

export function updateIncidentListSearch(
  searchParams: URLSearchParams,
  key: "q" | "status",
  value: string,
) {
  const next = new URLSearchParams(searchParams)
  if (value && !(key === "status" && value === "active")) next.set(key, value)
  else next.delete(key)
  return next
}
