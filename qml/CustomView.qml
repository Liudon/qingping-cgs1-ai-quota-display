import QtQuick 2.9
import "Quota.js" as Quota
import "Settings.js" as Settings

Item {
    id: page
    implicitWidth: 720
    implicitHeight: 720
    property var snapshot: null
    property var activeRequest: null
    property bool offline: false
    property string connectionMessage: Settings.serverUrl === "" ? "请配置额度地址" : "正在连接额度服务…"
    property string refreshIntervalLabel: Settings.refreshSeconds % 60 === 0 ? (Settings.refreshSeconds / 60) + " 分钟" : Settings.refreshSeconds + " 秒"
    property double clockOffset: 0
    property double now: Date.now() / 1000
    property int cycle: 0
    readonly property string textFont: "Source Han Sans CN"

    // 官方 MainPage 在滑动结束时调用；不抢占官方左右滑动手势。
    function restore() { }

    function fetchQuotas() {
        if (Settings.serverUrl === "" || activeRequest !== null) return
        var xhr = new XMLHttpRequest()
        activeRequest = xhr
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== 4 || page.activeRequest !== xhr) return
            requestDeadline.stop()
            page.activeRequest = null
            if (xhr.status === 200) {
                try {
                    var data = JSON.parse(xhr.responseText)
                    if (!Quota.validate(data)) throw new Error("schema")
                    var serverNow = Quota.responseTime(xhr.getResponseHeader("Date"), xhr.getResponseHeader("Age"), Date.now() / 1000, page.snapshot ? page.now : null)
                    page.clockOffset = serverNow - Date.now() / 1000
                    page.now = serverNow
                    page.snapshot = data
                    page.offline = false
                    page.connectionMessage = "已连接额度服务 · 每 " + page.refreshIntervalLabel + "读取"
                    return
                } catch (e) {
                    page.connectionMessage = "数据格式异常 · 保留上次数据"
                }
            } else {
                page.connectionMessage = "额度服务离线 · 保留上次数据"
            }
            page.offline = true
        }
        xhr.open("GET", Quota.requestUrl(Settings.serverUrl, Date.now()))
        xhr.setRequestHeader("Cache-Control", "no-cache")
        requestDeadline.start()
        xhr.send()
    }

    Timer {
        id: requestDeadline
        interval: 8000
        onTriggered: {
            var xhr = page.activeRequest
            page.activeRequest = null
            page.offline = true
            page.connectionMessage = "连接超时 · 保留上次数据"
            if (xhr !== null) xhr.abort()
        }
    }
    Timer {
        interval: Settings.refreshSeconds * 1000
        repeat: true
        running: page.visible && Settings.serverUrl !== ""
        triggeredOnStart: true
        onTriggered: page.fetchQuotas()
    }
    Timer {
        interval: 10000
        repeat: true
        running: page.visible
        onTriggered: { page.now = Date.now() / 1000 + page.clockOffset; page.cycle += 1 }
    }
    Component.onDestruction: {
        var xhr = activeRequest
        activeRequest = null
        if (xhr !== null) xhr.abort()
    }

    Text {
        x: 30; y: 88
        text: "AI 订阅额度"
        color: "white"
        font.family: page.textFont
        font.pixelSize: 30
    }
    Text {
        anchors.right: parent.right; anchors.rightMargin: 32; y: 98
        text: page.snapshot && page.snapshot.providers.length > 2 ? "剩余可用 · 上下滑动查看更多" : "剩余可用"
        color: "#a4b2bd"
        font.family: page.textFont
        font.pixelSize: 20
    }

    Flickable {
        id: providerScroll
        x: 30; y: 140
        width: parent.width - 60
        height: 500
        contentWidth: width
        contentHeight: Math.max(height, providerColumn.implicitHeight)
        clip: true
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds

        Column {
            id: providerColumn
            width: providerScroll.width
            spacing: 14
            Repeater {
                model: page.snapshot ? page.snapshot.providers : []
                delegate: Rectangle {
                    id: card
                    width: providerColumn.width
                    height: 242
                    radius: 18
                    color: "#d91c2730"
                    property var provider: modelData
                    property bool old: Quota.stale(provider, page.now, (page.snapshot ? page.snapshot.stale_after_seconds : 180) + Settings.refreshSeconds, page.offline)
                    Text {
                        x: 20; y: 12; width: parent.width - 190
                        text: card.provider.name
                        color: "white"
                        font.family: page.textFont
                        font.pixelSize: 28
                        elide: Text.ElideRight
                    }
                    Text {
                        anchors.right: parent.right; anchors.rightMargin: 20; y: 20
                        text: card.old ? "未同步 / 旧数据" : "已同步"
                        color: card.old ? "#f5bc67" : "#67d6a4"
                        font.family: page.textFont
                        font.pixelSize: 18
                    }
                    Column {
                        x: 20; y: 60
                        width: parent.width - 40
                        spacing: 5
                        Repeater {
                            model: Quota.visibleWindows(card.provider, page.cycle)
                            delegate: Item {
                                id: row
                                width: parent.width; height: 54
                                property var quota: modelData
                                Text {
                                    x: 0; y: 0; width: parent.width - 100
                                    text: row.quota.label
                                    color: card.old ? "#99a7b2" : "#eef3f6"
                                    font.family: page.textFont
                                    font.pixelSize: 22
                                    elide: Text.ElideRight
                                }
                                Text {
                                    anchors.right: parent.right; y: -3
                                    text: Math.round(row.quota.remaining_percent) + "%"
                                    color: Quota.color(row.quota.remaining_percent, card.old)
                                    font.family: page.textFont
                                    font.pixelSize: 28
                                }
                                Rectangle {
                                    y: 30; width: parent.width; height: 4; radius: 2; color: "#374650"
                                    Rectangle {
                                        width: parent.width * row.quota.remaining_percent / 100
                                        height: 4; radius: 2
                                        color: Quota.color(row.quota.remaining_percent, card.old)
                                    }
                                }
                                Text {
                                    y: 35; width: parent.width
                                    text: Quota.resetLabel(row.quota.resets_at, page.now)
                                    color: "#a4b2bd"
                                    font.family: page.textFont
                                    font.pixelSize: 15
                                }
                            }
                        }
                    }
                    Text {
                        x: 20; y: 90; width: parent.width - 40
                        visible: card.provider.windows.length === 0
                        text: card.provider.message
                        color: "#a4b2bd"
                        font.family: page.textFont
                        font.pixelSize: 22
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
    }
    Text {
        x: 30; y: 285; width: parent.width - 60
        visible: page.snapshot !== null && page.snapshot.providers.length === 0
        text: "暂无订阅额度信息"
        color: "#a4b2bd"
        font.family: page.textFont
        font.pixelSize: 22
        horizontalAlignment: Text.AlignHCenter
    }
    Text {
        x: 32; y: 654; width: parent.width - 64
        text: page.connectionMessage + (page.snapshot ? " · 数据 " + Qt.formatDateTime(new Date(page.snapshot.generated_at * 1000), "MM-dd hh:mm") : "")
        color: page.offline ? "#f5bc67" : "#a4b2bd"
        font.family: page.textFont
        font.pixelSize: 18
        elide: Text.ElideRight
    }
}
