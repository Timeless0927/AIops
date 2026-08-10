import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatRelativeTime(epochSeconds: number, now = Date.now()): string {
  const elapsed = Math.max(0, Math.floor((now - epochSeconds * 1000) / 1000))
  if (elapsed < 60) return "刚刚"
  const minutes = Math.floor(elapsed / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return new Date(epochSeconds * 1000).toLocaleDateString("zh-CN")
}
