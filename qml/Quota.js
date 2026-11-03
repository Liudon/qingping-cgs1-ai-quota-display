.pragma library

function validNumber(value) {
    return typeof value === "number" && isFinite(value)
}

// 静态 JSON 的生成时间不是当前时间：优先使用 HTTP Date + Age 校时。
// 反复读到同一份旧文件时，时间必须继续前进，才能正确显示过期状态。
function responseTime(dateHeader, ageHeader, localNow, previousNow) {
    var parsed = Date.parse(dateHeader) / 1000
    var age = Number(ageHeader)
    var current = validNumber(parsed) ? parsed + (validNumber(age) && age > 0 ? age : 0) : localNow
    return validNumber(previousNow) ? Math.max(current, previousNow) : current
}

function requestUrl(url, milliseconds) {
    return url + (url.indexOf("?") < 0 ? "?" : "&") + "_quota_t=" + milliseconds
}

function validate(data) {
    if (!data || data.schema_version !== 1 || !validNumber(data.generated_at) ||
            !validNumber(data.stale_after_seconds) || data.stale_after_seconds <= 0 ||
            !Array.isArray(data.providers))
        return false
    var ids = {}
    for (var i = 0; i < data.providers.length; i++) {
        var p = data.providers[i]
        if (!p || typeof p.id !== "string" || p.id.length === 0 || ids[p.id] ||
                typeof p.name !== "string" || typeof p.message !== "string" ||
                ["ok", "loading", "stale", "error"].indexOf(p.status) < 0 ||
                (p.updated_at !== null && !validNumber(p.updated_at)) ||
                (p.status === "ok" && p.updated_at === null) || !Array.isArray(p.windows))
            return false
        ids[p.id] = true
        for (var j = 0; j < p.windows.length; j++) {
            var w = p.windows[j]
            if (!w || typeof w.label !== "string" || !validNumber(w.remaining_percent) ||
                    w.remaining_percent < 0 || w.remaining_percent > 100 ||
                    (w.resets_at !== null && !validNumber(w.resets_at)))
                return false
        }
    }
    return true
}

function stale(provider, now, maxAge, offline) {
    if (!provider || offline || provider.status !== "ok" || provider.updated_at === null ||
            now - provider.updated_at > maxAge)
        return true
    for (var i = 0; i < provider.windows.length; i++) {
        var reset = provider.windows[i].resets_at
        if (reset !== null && reset <= now)
            return true
    }
    return false
}

function resetLabel(reset, now) {
    if (reset === null) return "重置时间未知"
    var minutes = Math.ceil((reset - now) / 60)
    if (minutes <= 0) return "等待额度刷新"
    if (minutes < 60) return minutes + " 分钟后重置"
    if (minutes < 1440) return Math.floor(minutes / 60) + " 小时 " + minutes % 60 + " 分后重置"
    return Math.floor(minutes / 1440) + " 天 " + Math.floor(minutes % 1440 / 60) + " 小时后重置"
}

function color(remaining, isStale) {
    return isStale ? "#82909c" : remaining < 10 ? "#f27d83" : remaining < 20 ? "#f5bc67" : "#67d6a4"
}

function visibleWindows(provider, cycle) {
    if (!provider) return []
    var windows = provider.windows
    var start = (cycle % Math.max(1, Math.ceil(windows.length / 3))) * 3
    return windows.slice(start, start + 3)
}
