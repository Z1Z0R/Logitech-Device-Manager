import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import QtQuick.Shapes
import Quickshell.Io
import qs.Commons as Commons
import qs.Ui as Ui

Ui.BarWidget {
    id: root
    moduleName: "logitech-device-manager"
    implicitWidth: badge.implicitWidth
    implicitHeight: barSize

    readonly property var themeOverrides: Commons.Color.userShellValues ?? ({})
    readonly property color panelForeground: !themeOverrides["popups.text"] && themeOverrides["bar.text"]
        ? Commons.Color.bar.text : Commons.Color.popups.text
    readonly property string panelFont: bar?.fontFamily || Commons.Style.font.family
    readonly property color panelBackground: {
        const popup = Commons.Color.popups.background
        const barColor = Commons.Color.bar.background
        const background = !themeOverrides["popups.background"] && themeOverrides["bar.background"]
            ? barColor : popup
        // Resolve color and alpha independently so a color override preserves theme opacity.
        const alpha = themeOverrides["popups.background-alpha"] ? popup.a
            : themeOverrides["bar.background-alpha"] ? barColor.a
            : popup.a
        return Qt.rgba(background.r, background.g, background.b, alpha)
    }

    // KeyboardPanel in Omarchy 4.5 does not expose its card fill as a property.
    // Only bind it when the content is hosted inside the standard BorderSurface.
    readonly property var panelSurface: deviceScroll.parent?.parent instanceof Ui.BorderSurface
        ? deviceScroll.parent.parent : null
    Binding {
        target: root.panelSurface
        property: "color"
        value: root.panelBackground
        when: root.panelSurface !== null
    }

    property bool opened: false
    property bool loaded: false
    property var devices: ({})
    property var deviceIds: []
    property var notices: []
    property double solaarHandoffSince: 0
    property var lastSet: ({})
    property var linkedDpi: ({})
    property var expandedDevices: ({})
    property var fastBatteries: null
    property var fastBatteriesByIdentity: ({})
    property var pending: null
    property string feedback: ""
    property string dpiBlockedDevice: ""
    readonly property var rgbColors: [
        {name: "red", color: "#FF4040"}, {name: "orange", color: "#FF8C24"},
        {name: "yellow", color: "#FFD84A"}, {name: "lime", color: "#B5E845"},
        {name: "green", color: "#35D06A"}, {name: "mint", color: "#46E6AE"},
        {name: "cyan", color: "#40D9F5"}, {name: "blue", color: "#4285FF"},
        {name: "indigo", color: "#635BFF"}, {name: "purple", color: "#AF62FF"},
        {name: "pink", color: "#FF62AE"}, {name: "white", color: "#FFFFFF"}
    ]
    readonly property var helper: ["timeout", "12s", "python3", "-B",
        decodeURIComponent(Qt.resolvedUrl("backend.py").toString().replace(/^file:\/\//, ""))]

    Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

    function close() {
        opened = false
        fastBatteries = null
        fastBatteriesByIdentity = {}
        solaarHandoffSince = 0
    }

    function eqBandName(index, count) {
        if (count <= 1) return strings.t("eqBand.overall")
        const names = count <= 5
            ? ["bass", "lowMids", "mids", "highMids", "treble"]
            : ["subBass", "bass", "upperBass", "lowMids", "mids",
                "highMids", "presence", "lowerTreble", "treble", "air"]
        const position = Math.round(index * (names.length - 1) / (count - 1))
        const label = strings.t("eqBand." + names[position])
        return count > names.length ? strings.t("eqBand.numbered").arg(label).arg(index + 1) : label
    }

    function toggleSettings(id) {
        const next = Object.assign({}, expandedDevices)
        if (next[id]) delete next[id]
        else next[id] = true
        expandedDevices = next
    }

    function batteryFor(device) {
        const reported = device?.battery
        const exact = device?.batteryKey ? fastBatteries?.[device.batteryKey] : null
        const alternate = device?.batteryIdentity ? fastBatteriesByIdentity[device.batteryIdentity] : null
        const fast = exact || alternate
        if (!fast) {
            if (fastBatteries && device?.kernelBattery && reported?.state.startsWith("Charging"))
                return {state: strings.t("reconnecting"), level: reported.level, approximate: reported.approximate}
            return reported
        }
        // The kernel can report charging immediately while its percentage
        // remains cached. Prefer the directly queried level on this transport.
        const useReportedLevel = reported?.directLevel && (exact || !alternate)
        return {
            state: fast.state === "Unknown" ? (reported?.state || fast.state) : fast.state,
            level: useReportedLevel ? reported.level : (fast.level ?? reported?.level ?? null),
            approximate: (useReportedLevel || fast.level == null) && !!reported?.approximate
        }
    }

    Component.onCompleted: root.scan()

    function scan() {
        if (!reader.running && pending === null) reader.running = true
    }

    function apply(device, key, value, route = device) {
        if (pending !== null) return
        pending = {device: device, route: route, key: key, value: value}
        feedback = ""
        dpiBlockedDevice = ""
        if (!reader.running) writer.running = true
    }

    function readResult(output) {
        if (output.length > 524288) throw new Error("Oversized hardware response")
        return JSON.parse(output)
    }

    function updateDevices(data, provisional = false) {
        if (!Array.isArray(data.devices)) throw new Error("Invalid hardware response")
        const byId = {}
        const ids = []
        for (const device of data.devices.slice(0, 32)) {
            if (byId[device.id]) continue
            ids.push(device.id)
            byId[device.id] = device
        }
        devices = byId
        if (JSON.stringify(deviceIds) !== JSON.stringify(ids)) deviceIds = ids
        if (provisional) return
        const nextNotices = (data.notices || []).slice()
        if (data.solaarHandoff) {
            const now = Date.now()
            if (solaarHandoffSince === 0) solaarHandoffSince = now
            if (now - solaarHandoffSince >= 2500) nextNotices.push(data.solaarHandoff)
        } else {
            solaarHandoffSince = 0
        }
        notices = nextNotices
        loaded = true
    }

    function confirmSetting(id, key, result) {
        const previous = devices[id]
        if (!previous) return

        const updated = Object.assign({}, previous)
        const dpiValues = result.dpiValues || {}
        if (key === "dpi_link") updated.dpiLinked = result.value
        if (key === "dpi_extended:both") {
            dpiValues["dpi_extended:0"] = result.value
            dpiValues["dpi_extended:1"] = result.value
        }
        updated.controls = previous.controls.map(control => {
            if (key === "mute" && control.key === "volume" && typeof result.muted === "boolean")
                return Object.assign({}, control, {muted: result.muted})
            if (control.key in dpiValues)
                return Object.assign({}, control, {value: dpiValues[control.key]})
            if (control.key !== key || control.cached || !("value" in result)) return control
            const confirmed = Object.assign({}, control, {value: result.value})
            if ("color" in result) confirmed.color = result.color
            if ("speed" in result) confirmed.speed = result.speed
            return confirmed
        })
        devices = Object.assign({}, devices, {[id]: updated})
    }

    function remembered(id, key) {
        return lastSet[JSON.stringify([id, key])]
    }

    Process {
        id: reader
        command: root.helper.concat(["scan"])
        stdout: StdioCollector { id: scanOutput }
        onExited: function(code) {
            try {
                const result = root.readResult(scanOutput.text)
                if (code !== 0 || result.error) throw new Error("Scan failed")
                root.updateDevices(result)
            } catch (error) {
                root.notices = [strings.t("error")]
            }
            if (root.pending !== null) writer.running = true
        }
    }

    Process {
        id: writer
        command: root.pending ? root.helper.concat(["set", root.pending.route,
            root.pending.key, JSON.stringify(root.pending.value)]) : []
        stdout: StdioCollector { id: writeOutput }
        onExited: function(code) {
            try {
                const data = root.readResult(writeOutput.text)
                if (code !== 0) throw new Error("Write failed")
                if (data.errorCode === "dpiOnboardProfile") {
                    root.dpiBlockedDevice = root.pending.device
                    root.feedback = strings.t("dpiOnboardProfile")
                } else {
                    root.feedback = data.error || strings.t(root.pending.key === "dpi_link" ? "dpiLinkSaved" : "applied")
                }
                if (!data.error) {
                    root.confirmSetting(root.pending.device, root.pending.key, data)
                    const cache = Object.assign({}, root.lastSet)
                    cache[JSON.stringify([root.pending.device, root.pending.key])] = data
                    root.lastSet = cache
                }
            } catch (error) {
                root.feedback = strings.t("error")
            }
            root.pending = null
            if (root.opened) Qt.callLater(root.scan)
        }
    }

    Process {
        id: batteryReader
        command: root.helper.concat(["battery"])
        stdout: StdioCollector { id: batteryOutput }
        onExited: function(code) {
            if (!root.opened) return
            try {
                const result = root.readResult(batteryOutput.text)
                root.fastBatteries = code === 0 && result.batteries ? result.batteries : null
                root.fastBatteriesByIdentity = root.fastBatteries ? (result.batteriesByIdentity || {}) : {}
                if (!root.loaded && code === 0 && Array.isArray(result.devices))
                    root.updateDevices(result, true)
            } catch (error) {
                root.fastBatteries = null
                root.fastBatteriesByIdentity = {}
            }
        }
    }

    Timer {
        interval: 400
        repeat: true
        running: root.opened
        triggeredOnStart: true
        onTriggered: if (!batteryReader.running) batteryReader.running = true
    }

    Timer {
        interval: 1250
        repeat: true
        running: root.opened
        triggeredOnStart: true
        onTriggered: root.scan()
    }

    Ui.BarIconButton {
        id: badge
        anchors.centerIn: parent
        bar: root.bar
        objectName: "logitechDeviceManagerBarIcon"
        iconComponent: Shape {
            width: 24
            height: 24

            ShapePath {
                strokeWidth: -1
                fillColor: badge.active ? badge.activeColor : badge.foreground
                PathSvg {
                    path: "M 0.5,10 C 1.5,4.3 6.1,0.5 12,0.5 C 15.5,0.5 18.5,1.9 20.7,4.2 L 16.9,7.9 C 15.6,6.5 13.9,5.8 12,5.8 C 9.2,5.8 6.8,7.5 5.9,10 Z M 0.5,12 H 5.8 C 5.8,15.6 8.4,18.2 12,18.2 C 14.6,18.2 16.7,16.8 17.6,14.8 H 12 V 10 H 23.5 V 12 C 23.5,18.5 18.6,23.5 12,23.5 C 5.6,23.5 0.5,18.5 0.5,12 Z"
                }
            }
        }
        active: root.opened
        tooltipText: strings.t("title")
        onPressed: function(button) {
            if (button !== Qt.LeftButton) return
            if (root.opened) root.close()
            else root.opened = true
        }
    }

    component Plain: Text {
        textFormat: Text.PlainText
        color: root.panelForeground
        font.family: root.panelFont
        font.pixelSize: Commons.Style.font.body
        wrapMode: Text.Wrap
    }

    component Choice: Controls.ComboBox {
        id: choice
        property string emptyLabel: strings.t("unknown")
        textRole: "label"
        valueRole: "value"
        popup.popupType: Controls.Popup.Item
        popup.background: Rectangle {
            color: root.panelBackground
            border.color: root.panelForeground
            radius: Commons.Style.cornerRadius
        }
        contentItem: Plain {
            leftPadding: 8
            rightPadding: choice.indicator ? choice.indicator.width + 6 : 8
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            wrapMode: Text.NoWrap
            text: choice.currentIndex >= 0 ? choice.currentText : choice.emptyLabel
        }
        background: Rectangle {
            color: "transparent"
            border.color: choice.activeFocus ? Commons.Color.accent : root.panelForeground
            radius: Commons.Style.cornerRadius
        }
        delegate: Controls.ItemDelegate {
            required property var modelData
            required property int index
            width: choice.width
            highlighted: choice.highlightedIndex === index
            background: Rectangle {
                color: parent.highlighted ? Qt.alpha(Commons.Color.accent, 0.30) : "transparent"
            }
            contentItem: Plain {
                text: modelData.label
                elide: Text.ElideRight
                wrapMode: Text.NoWrap
            }
        }
    }

    Ui.KeyboardPanel {
        anchorItem: badge
        bar: root.bar
        owner: root
        open: root.opened
        focusTarget: deviceScroll
        contentWidth: fittedContentWidth(Commons.Style.space(1020))
        contentHeight: fittedContentHeight(panel.implicitHeight, Commons.Style.space(580))

        Flickable {
            id: deviceScroll
            anchors.fill: parent
            Keys.onEscapePressed: root.close()
            clip: true
            contentWidth: width
            contentHeight: panel.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            Controls.ScrollBar.vertical: Controls.ScrollBar {
                policy: parent && size < 1 ? Controls.ScrollBar.AlwaysOn : Controls.ScrollBar.AlwaysOff
            }

            ColumnLayout {
                id: panel
                width: parent.width
                spacing: Commons.Style.space(10)

                RowLayout {
                    Layout.fillWidth: true
                    Plain {
                        Layout.fillWidth: true
                        text: strings.t("title")
                        font.bold: true
                        font.pixelSize: Commons.Style.font.title
                    }
                    Ui.Button {
                        foreground: root.panelForeground
                        fontFamily: root.panelFont
                        text: strings.t("refresh")
                        focusable: true
                        enabled: !reader.running && root.pending === null
                        onClicked: root.scan()
                    }
                }
                Plain {
                    Layout.fillWidth: true
                    opacity: 0.7
                    visible: text.length > 0
                    text: root.pending ? strings.t("saving") : (root.feedback ||
                        (!root.loaded && root.deviceIds.length > 0 ? strings.t("loadingSettings") : ""))
                }
                Ui.Button {
                    foreground: root.panelForeground
                    fontFamily: root.panelFont
                    readonly property var profile: root.devices[root.dpiBlockedDevice]?.controls.find(c => c.key === "onboard_profiles")
                    visible: !!root.dpiBlockedDevice && !!profile?.value && !!profile?.options?.some(o => o.value === 0)
                    enabled: root.pending === null
                    text: strings.t("disableOnboardProfile")
                    focusable: true
                    onClicked: root.apply(root.dpiBlockedDevice, "onboard_profiles", 0)
                }
                Plain {
                    Layout.fillWidth: true
                    visible: root.deviceIds.length === 0
                    text: strings.t(root.loaded ? "empty" : "scanning")
                }
                Repeater {
                    model: root.notices
                    Plain {
                        required property string modelData
                        Layout.fillWidth: true
                        opacity: 0.75
                        text: modelData
                    }
                }
                Repeater {
                    model: root.deviceIds
                    ColumnLayout {
                        id: deviceCard
                        required property string modelData
                        readonly property string deviceId: modelData
                        readonly property var device: root.devices[deviceId]
                        property var keys: []
                        property bool hitsOpen: false
                        readonly property var hitsKeys: keys.filter(key => key.startsWith("analog-button-tuning_"))
                        readonly property string deviceRole: deviceId.startsWith("battery:") ? "input" : deviceId.split(":")[0]
                        readonly property string settingsKey: device?.hardwareId
                            ? JSON.stringify([deviceRole, device.hardwareId]) : deviceId
                        readonly property bool settingsOpen: !!root.expandedDevices[settingsKey]
                        readonly property var combinedDpi: device?.controls.find(c => c.key === "dpi_extended:both")
                        readonly property bool separateDpi: keys.includes("dpi_extended:1")
                        readonly property bool hasDpi: !!device?.controls.some(control => control.type === "dpi")
                        readonly property bool hasPolling: !!device?.controls.some(control => control.type === "choice"
                            && control.key.startsWith("report_rate"))
                        readonly property bool hasLighting: !!device?.controls.some(control => control.type === "rgb")
                        readonly property var onboardProfile: device?.controls.find(control =>
                            control.key === "onboard_profiles" && control.type === "choice")
                        readonly property bool onboardActive: onboardProfile?.value > 0
                        readonly property bool dpiLinkPending: root.pending?.device === deviceId
                            && root.pending?.key === "dpi_link"
                        readonly property bool dpiLinked: !!combinedDpi && (dpiLinkPending
                            ? root.pending.value : (device?.canSaveDpiLink
                                ? !!device.dpiLinked : !!root.linkedDpi[deviceId]))

                        function toggleDpiLink() {
                            if (device.canSaveDpiLink) {
                                root.apply(deviceId, "dpi_link", !dpiLinked)
                                return
                            }
                            const next = Object.assign({}, root.linkedDpi)
                            if (dpiLinked) {
                                delete next[deviceId]
                            } else {
                                next[deviceId] = true
                                if (combinedDpi.value === null && combinedDpi.linkValue != null)
                                    root.apply(deviceId, combinedDpi.key, combinedDpi.linkValue)
                            }
                            root.linkedDpi = next
                        }

                        function syncKeys() {
                            const controls = device?.controls || []
                            const dpi = controls.filter(control => control.type === "dpi")
                            const others = controls.filter(control => control.type !== "dpi")
                            const next = dpi.concat(others).map(control => control.key)
                            if (JSON.stringify(keys) !== JSON.stringify(next)) keys = next
                        }
                        onDeviceChanged: syncKeys()
                        Component.onCompleted: syncKeys()
                        Layout.fillWidth: true
                        spacing: Commons.Style.space(6)

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: 1
                            color: root.panelForeground
                            opacity: 0.22
                        }
                        GridLayout {
                            id: deviceColumns
                            readonly property bool sideBySide: width >= Commons.Style.space(480)
                            Layout.fillWidth: true
                            columns: sideBySide ? 3 : 1
                            columnSpacing: Commons.Style.space(12)
                            rowSpacing: Commons.Style.space(6)

                            ColumnLayout {
                                Layout.alignment: Qt.AlignTop
                                Layout.fillWidth: !deviceColumns.sideBySide || !deviceCard.settingsOpen
                                Layout.preferredWidth: deviceColumns.sideBySide ? Commons.Style.space(172) : -1
                                Layout.maximumWidth: deviceColumns.sideBySide && deviceCard.settingsOpen
                                    ? Commons.Style.space(172) : Number.POSITIVE_INFINITY
                                spacing: Commons.Style.space(4)
                                Plain {
                                    Layout.fillWidth: true
                                    font.bold: true
                                    text: deviceCard.device?.name || ""
                                }
                                Plain {
                                    Layout.fillWidth: true
                                    opacity: 0.75
                                    text: deviceCard.device?.kind || ""
                                }
                                Plain {
                                    Layout.fillWidth: true
                                    readonly property var battery: root.batteryFor(deviceCard.device)
                                    visible: !!battery && deviceCard.device?.kind !== "Headset lighting"
                                    text: battery ? strings.t("battery").arg(battery.level === null
                                        ? strings.t("unknown") : (battery.approximate ? "≈" : "")
                                        + Commons.I18n.formatNumber(battery.level) + "%").arg(battery.state) : ""
                                }
                                Plain {
                                    visible: root.loaded && deviceCard.keys.length === 0
                                    text: strings.t("noControls")
                                }
                                Controls.AbstractButton {
                                    id: disclosure
                                    Layout.fillWidth: true
                                    implicitHeight: Commons.Style.space(22)
                                    visible: deviceCard.keys.length > 0
                                    hoverEnabled: true
                                    Accessible.name: strings.t(deviceCard.settingsOpen ? "hideSettings" : "showSettings")
                                    onClicked: root.toggleSettings(deviceCard.settingsKey)
                                    background: Item {}
                                    contentItem: RowLayout {
                                        spacing: Commons.Style.space(8)
                                        Rectangle {
                                            Layout.fillWidth: true
                                            implicitHeight: 1
                                            color: disclosure.activeFocus ? Commons.Color.accent : root.panelForeground
                                            opacity: disclosure.hovered || disclosure.activeFocus ? 0.8 : 0.32
                                        }
                                        Plain {
                                            text: "›"
                                            rotation: deviceCard.settingsOpen ? 90 : (deviceColumns.sideBySide ? 0 : 180)
                                            font.bold: true
                                        }
                                    }
                                }
                            }

                            Rectangle {
                                visible: deviceCard.settingsOpen
                                Layout.fillWidth: !deviceColumns.sideBySide
                                Layout.fillHeight: deviceColumns.sideBySide
                                Layout.preferredWidth: 1
                                Layout.preferredHeight: 1
                                Layout.minimumWidth: 1
                                Layout.minimumHeight: 1
                                color: root.panelForeground
                                opacity: 0.26
                            }

                            GridLayout {
                                id: settingsGrid
                                Layout.fillWidth: true
                                Layout.alignment: Qt.AlignTop
                                visible: deviceCard.settingsOpen
                                columns: deviceColumns.width >= Commons.Style.space(740)
                                    && deviceCard.keys.length > 1 ? 2 : 1
                                uniformCellWidths: true
                                columnSpacing: Commons.Style.space(18)
                                rowSpacing: Commons.Style.space(columns > 1 ? 8 : 6)
                                Ui.Toggle {
                                    Layout.columnSpan: settingsGrid.columns
                                    foreground: root.panelForeground
                                    fontFamily: root.panelFont
                                    Layout.fillWidth: true
                                    visible: deviceCard.settingsOpen && !!deviceCard.combinedDpi
                                    enabled: root.pending === null
                                    label: strings.t("linkDpi")
                                    checked: deviceCard.dpiLinked
                                    onClicked: deviceCard.toggleDpiLink()
                                }
                                Plain {
                                    Layout.columnSpan: settingsGrid.columns
                                    Layout.fillWidth: true
                                    visible: deviceCard.settingsOpen && !!deviceCard.combinedDpi
                                    opacity: 0.75
                                    text: strings.t(deviceCard.dpiLinked ? "dpiLinkedState" : "dpiIndependentState")
                                }
                                Plain {
                                    Layout.columnSpan: settingsGrid.columns
                                    Layout.fillWidth: true
                                    visible: deviceCard.settingsOpen && deviceCard.hasDpi && !deviceCard.combinedDpi
                                    opacity: 0.75
                                    text: strings.t(deviceCard.separateDpi ? "noSharedDpi" : "singleDpi")
                                }
                                Plain {
                                    Layout.columnSpan: settingsGrid.columns
                                    Layout.fillWidth: true
                                    visible: deviceCard.settingsOpen && deviceCard.dpiLinked && !deviceCard.dpiLinkPending
                                        && deviceCard.combinedDpi?.value === null
                                    text: strings.t("chooseLinkedDpi")
                                }
                                Repeater {
                                    model: deviceCard.keys.filter(key => !key.startsWith("analog-button-tuning_"))
                                    ColumnLayout {
                                        id: row
                                        required property string modelData
                                        readonly property var spec: deviceCard.device?.controls.find(c => c.key === modelData) || {}
                                        readonly property var saved: spec.cached ? root.remembered(deviceCard.deviceId, modelData) : null
                                        readonly property var current: spec.value ?? saved?.value ?? null
                                        readonly property string currentColor: spec.color || saved?.color || ""
                                        readonly property var options: spec.options || []
                                        readonly property bool webcamNumber: deviceCard.device?.kind === "Webcam" && spec.type === "number"
                                        readonly property var currentOption: options.find(o => o.value === current)
                                        property var draftEffect: null
                                        property string draftEqProfile: ""
                                        property var draftEqBands: null
                                        readonly property string eqProfile: draftEqProfile || current || ""
                                        readonly property var eqBands: draftEqBands || spec.custom || []

                                        function editEqBand(index, gain) {
                                            const bands = eqBands.slice()
                                            bands[index] = gain
                                            draftEqBands = bands
                                        }
                                        property string draftColor: ""
                                        property var draftSpeed: null
                                        readonly property var speedSpec: selectedOption?.speed
                                        readonly property int selectedSpeed: {
                                            if (draftSpeed !== null) return draftSpeed
                                            const currentSpeed = spec.speed ?? saved?.speed
                                            if (selectedOption?.value === current && currentSpeed != null) return currentSpeed
                                            if (!speedSpec) return 0
                                            const fraction = (speedSpec.default - speedSpec.min) / (speedSpec.max - speedSpec.min)
                                            return Math.round(100 * (speedSpec.field === "period" ? 1 - fraction : fraction))
                                        }
                                        readonly property bool showColorPresets: spec.type === "rgb"
                                            && !!spec.software && !!selectedOption?.color
                                        readonly property var selectedOption: options.find(o => o.value === (draftEffect ?? current))
                                        Layout.fillWidth: true
                                        Layout.alignment: Qt.AlignTop
                                        visible: deviceCard.settingsOpen && (modelData === "dpi_extended:both" ? deviceCard.dpiLinked
                                            : !deviceCard.dpiLinked || (modelData !== "dpi_extended:0" && modelData !== "dpi_extended:1"))
                                        enabled: root.pending === null && spec.writable !== false

                                        function submit(value) { root.apply(deviceCard.deviceId, modelData, value, row.spec.route ?? deviceCard.deviceId) }

                                        GridLayout {
                                            columns: settingsGrid.columns === 2 ? 1 : 2
                                            rowSpacing: Commons.Style.space(2)
                                            Layout.fillWidth: true
                                            Plain {
                                                Layout.fillWidth: settingsGrid.columns === 2
                                                text: row.modelData === "dpi_extended:both" ? strings.t("linkedDpi") : (row.spec.label || "")
                                            }
                                            Plain {
                                                Layout.fillWidth: true
                                                visible: text.length > 0
                                                font.pixelSize: Commons.Style.font.caption
                                                opacity: 0.75
                                                text: {
                                                    if (row.modelData === "onboard_profiles" && deviceCard.onboardProfile) {
                                                        const affected = []
                                                        if (deviceCard.hasDpi) affected.push(strings.t("onboardDpi"))
                                                        if (deviceCard.hasPolling) affected.push(strings.t("onboardPolling"))
                                                        if (deviceCard.hasLighting) affected.push(strings.t("onboardLighting"))
                                                        if (affected.length) return strings.t("onboardAffected").arg(affected.join(", "))
                                                    }
                                                    if (deviceCard.onboardActive && (row.spec.type === "dpi"
                                                        || row.modelData.startsWith("report_rate")))
                                                        return strings.t("onboardActiveHint")
                                                    if (row.modelData === "rgb_control" && deviceCard.onboardProfile)
                                                        return strings.t("rgbProfileModeHint")
                                                    if (["rgb_control", "led_control", "headset_led_control"].includes(row.modelData))
                                                        return strings.t("lightingModeHint")
                                                    if (row.modelData === "preset" && row.spec.type === "choice")
                                                        return strings.t("headsetFactoryEqHint")
                                                    return ""
                                                }
                                            }
                                        }
                                        Choice {
                                            id: picker
                                            visible: row.spec.type === "choice" || row.spec.type === "rgb"
                                            Layout.fillWidth: true
                                            enabled: row.spec.type !== "rgb" || row.spec.software
                                            model: row.options
                                            currentIndex: row.options.findIndex(o => o.value === (row.draftEffect ?? row.current))
                                            onActivated: function(index) {
                                                const value = row.options[index].value
                                                if (row.spec.type === "rgb") {
                                                    row.draftEffect = value
                                                    row.draftSpeed = null
                                                } else row.submit(value)
                                            }
                                        }
                                        Choice {
                                            visible: row.spec.type === "eq"
                                            Layout.fillWidth: true
                                            emptyLabel: strings.t("eq.choose")
                                            model: row.options
                                            currentIndex: row.options.findIndex(o => o.value === row.eqProfile)
                                            onActivated: function(index) {
                                                const profile = row.options[index]?.value
                                                if (!profile) return
                                                row.draftEqProfile = profile === "custom" ? profile : ""
                                                if (profile !== "custom") row.submit({profile: profile})
                                            }
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.type === "eq" && !!row.eqProfile
                                            opacity: 0.75
                                            text: row.eqProfile ? strings.t("eq." + row.eqProfile) : ""
                                        }
                                        ColumnLayout {
                                            visible: row.spec.type === "eq" && row.eqProfile === "custom"
                                            Layout.fillWidth: true
                                            spacing: Commons.Style.space(3)
                                            Repeater {
                                                model: row.spec.type === "eq" ? row.spec.bands : 0
                                                RowLayout {
                                                    required property int index
                                                    Layout.fillWidth: true
                                                    Plain {
                                                        Layout.preferredWidth: Commons.Style.space(120)
                                                        text: root.eqBandName(index, row.spec.bands)
                                                    }
                                                    Ui.PanelSlider {
                                                        id: bandSlider
                                                        Layout.fillWidth: true
                                                        bar: root.bar
                                                        minimum: row.spec.min ?? 0
                                                        maximum: row.spec.max ?? 0
                                                        step: row.spec.step ?? 1
                                                        value: row.eqBands[index] ?? 0
                                                        onReleased: function(gain) { row.editEqBand(index, gain) }
                                                    }
                                                    Plain {
                                                        Layout.preferredWidth: Commons.Style.space(62)
                                                        horizontalAlignment: Text.AlignRight
                                                        text: strings.t("eqGain").arg((bandSlider.liveValue > 0 ? "+" : "")
                                                            + String(Math.round(bandSlider.liveValue * 1000) / 1000))
                                                    }
                                                }
                                            }
                                            Ui.Button {
                                                foreground: root.panelForeground
                                                fontFamily: root.panelFont
                                                Layout.alignment: Qt.AlignRight
                                                text: strings.t("applyEq")
                                                focusable: true
                                                onClicked: row.submit({profile: "custom", bands: row.eqBands})
                                            }
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.modelData === "inactive"
                                            opacity: 0.75
                                            text: strings.t("inactiveHint")
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.type === "rgb" || row.spec.type === "number" || (row.modelData === "preset" && !!row.saved)
                                            opacity: 0.75
                                            text: {
                                                const value = row.webcamNumber && webcamSlider.dragging
                                                    ? webcamSlider.controlValue(webcamSlider.liveValue)
                                                    : row.currentOption?.label ?? row.current ?? strings.t("unknown")
                                                const display = row.currentColor ? value + " · " + row.currentColor : value
                                                return strings.t(row.saved ? "last" : "current").arg(display)
                                            }
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.type === "rgb" && !row.spec.software
                                            text: strings.t("lighting")
                                            opacity: 0.75
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.type === "rgb" && !!row.spec.software && !!row.speedSpec
                                            text: strings.t("patternSpeed").arg(
                                                Math.round(patternSpeed.dragging ? patternSpeed.liveValue : row.selectedSpeed))
                                        }
                                        Ui.PanelSlider {
                                            id: patternSpeed
                                            visible: row.spec.type === "rgb" && !!row.spec.software && !!row.speedSpec
                                            Layout.fillWidth: true
                                            bar: root.bar
                                            minimum: 0
                                            maximum: 100
                                            step: 1
                                            integer: true
                                            value: row.selectedSpeed
                                            onReleased: function(value) { row.draftSpeed = Math.round(value) }
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.showColorPresets
                                            text: strings.t("colorPresets")
                                        }
                                        Flow {
                                            Layout.fillWidth: true
                                            visible: row.showColorPresets
                                            spacing: Commons.Style.space(5)
                                            Repeater {
                                                model: root.rgbColors
                                                Controls.AbstractButton {
                                                    id: swatch
                                                    required property var modelData
                                                    readonly property string colorName: strings.t("color." + modelData.name)
                                                    implicitWidth: Commons.Style.space(26)
                                                    implicitHeight: Commons.Style.space(26)
                                                    hoverEnabled: true
                                                    focusPolicy: Qt.StrongFocus
                                                    Accessible.name: colorName
                                                    Controls.ToolTip.text: colorName
                                                    Controls.ToolTip.visible: hovered
                                                    Controls.ToolTip.delay: 400
                                                    onClicked: {
                                                        row.draftColor = modelData.color
                                                        rgbField.text = modelData.color
                                                    }
                                                    background: Rectangle {
                                                        radius: Commons.Style.cornerRadius
                                                        color: swatch.modelData.color
                                                        border.width: swatch.activeFocus || swatch.hovered
                                                            || row.draftColor.toUpperCase() === swatch.modelData.color ? 2 : 1
                                                        border.color: root.panelForeground
                                                    }
                                                }
                                            }
                                        }
                                        RowLayout {
                                            visible: row.spec.type === "rgb" && !!row.spec.software
                                            Layout.fillWidth: true
                                            Rectangle {
                                                visible: !!row.currentColor
                                                implicitWidth: 22
                                                implicitHeight: 22
                                                radius: Commons.Style.cornerRadius
                                                color: row.currentColor || "transparent"
                                                border.color: root.panelForeground
                                            }
                                            Controls.TextField {
                                                id: rgbField
                                                color: root.panelForeground
                                                placeholderTextColor: Qt.alpha(root.panelForeground, 0.75)
                                                background: Rectangle {
                                                    radius: Commons.Style.cornerRadius
                                                    color: "transparent"
                                                    border.color: rgbField.activeFocus ? Commons.Color.accent
                                                        : Qt.alpha(root.panelForeground, 0.65)
                                                }
                                                visible: !!row.selectedOption?.color
                                                Layout.fillWidth: true
                                                selectByMouse: true
                                                placeholderText: row.currentColor || "#FFFFFF"
                                                maximumLength: 7
                                                validator: RegularExpressionValidator { regularExpression: /#[0-9a-fA-F]{6}/ }
                                                onTextEdited: row.draftColor = text
                                            }
                                            Ui.Button {
                                                foreground: root.panelForeground
                                                fontFamily: root.panelFont
                                                text: strings.t("apply")
                                                focusable: true
                                                enabled: !!row.selectedOption && (!row.selectedOption.color || !row.draftColor
                                                    || /^#[0-9a-fA-F]{6}$/.test(row.draftColor))
                                                onClicked: row.submit({effect: row.selectedOption.value,
                                                    color: row.draftColor || row.currentColor || "#FFFFFF",
                                                    speed: row.speedSpec ? row.selectedSpeed : null})
                                            }
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.type === "dpi"
                                            opacity: 0.75
                                            text: strings.t("dpiRange")
                                                .arg(row.current === null ? strings.t("unknown") : Commons.I18n.formatNumber(row.current))
                                                .arg(Commons.I18n.formatNumber(row.spec.min ?? 0))
                                                .arg(Commons.I18n.formatNumber(row.spec.max ?? 0))
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.step != null && row.spec.type === "number"
                                            opacity: 0.75
                                            text: strings.t("cameraRange").arg(row.spec.min ?? 0)
                                                .arg(row.spec.max ?? 0).arg(row.spec.step ?? 1)
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.writable === false
                                            opacity: 0.75
                                            text: strings.t("cameraInactive")
                                        }
                                        Ui.Button {
                                            visible: row.spec.type === "camera_action"
                                            foreground: root.panelForeground
                                            fontFamily: root.panelFont
                                            text: strings.t("cameraReset")
                                            focusable: true
                                            onClicked: row.submit(true)
                                        }
                                        RowLayout {
                                            visible: (row.spec.type === "number" && !row.webcamNumber) || row.spec.type === "dpi"
                                            Layout.fillWidth: true
                                            Controls.TextField {
                                                id: numberField
                                                readonly property bool validValue: /^-?[0-9]+$/.test(text)
                                                    && Number(text) >= (row.spec.min ?? 0) && Number(text) <= (row.spec.max ?? 0)
                                                    && (Number(text) - (row.spec.min ?? 0)) % (row.spec.step ?? 1) === 0
                                                Layout.fillWidth: true
                                                Layout.minimumHeight: row.spec.type === "dpi" ? Commons.Style.space(34) : 0
                                                focusPolicy: Qt.StrongFocus
                                                activeFocusOnPress: true
                                                selectByMouse: true
                                                inputMethodHints: (row.spec.min ?? 0) < 0 ? Qt.ImhFormattedNumbersOnly : Qt.ImhDigitsOnly
                                                placeholderText: row.spec.type === "dpi" ? strings.t("typeDpi")
                                                    : (row.current === null ? strings.t("unknown") : String(row.current))
                                                Accessible.name: row.spec.label || ""
                                                color: root.panelForeground
                                                placeholderTextColor: Qt.alpha(root.panelForeground, 0.75)
                                                background: Rectangle {
                                                    radius: Commons.Style.cornerRadius
                                                    color: "transparent"
                                                    border.width: numberField.activeFocus ? 2 : 1
                                                    border.color: numberField.activeFocus ? Commons.Color.accent
                                                        : Qt.alpha(root.panelForeground, 0.65)
                                                }
                                                maximumLength: Math.max(String(row.spec.min ?? 0).length, String(row.spec.max ?? 0).length)
                                                validator: RegularExpressionValidator {
                                                    regularExpression: (row.spec.min ?? 0) < 0 ? /-?[0-9]*/ : /[0-9]*/
                                                }
                                                onAccepted: if (validValue) row.submit(Number(text))
                                            }
                                            Ui.Button {
                                                foreground: root.panelForeground
                                                fontFamily: root.panelFont
                                                text: strings.t("apply")
                                                focusable: true
                                                enabled: numberField.validValue
                                                onClicked: row.submit(Number(numberField.text))
                                            }
                                        }
                                        Ui.PanelSlider {
                                            id: webcamSlider
                                            visible: row.webcamNumber
                                            enabled: maximum > minimum
                                            Layout.fillWidth: true
                                            bar: root.bar
                                            minimum: 0
                                            maximum: Math.floor(((row.spec.max ?? 0) - (row.spec.min ?? 0)) / increment)
                                            step: 1
                                            integer: true
                                            readonly property real increment: row.spec.step ?? 1
                                            value: ((row.current ?? row.spec.min ?? 0) - (row.spec.min ?? 0)) / increment
                                            Accessible.role: Accessible.Slider
                                            Accessible.name: row.spec.label || ""

                                            function controlValue(position) {
                                                return (row.spec.min ?? 0) + position * increment
                                            }

                                            onReleased: function(position) {
                                                const value = controlValue(position)
                                                if (value !== row.current) row.submit(value)
                                            }
                                        }
                                        Ui.PanelSlider {
                                            id: microphoneGain
                                            visible: row.spec.type === "gain"
                                            Layout.fillWidth: true
                                            bar: root.bar
                                            minimum: row.spec.min ?? 0
                                            maximum: row.spec.max ?? 100
                                            step: 1
                                            integer: true
                                            value: row.current ?? minimum
                                            onReleased: function(value) { row.submit(Math.round(value)) }
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.type === "gain"
                                            text: strings.t("volume").arg(Commons.I18n.formatNumber(
                                                microphoneGain.dragging ? microphoneGain.liveValue : (row.current ?? 0)))
                                        }
                                        RowLayout {
                                            visible: row.spec.type === "slider"
                                            Layout.fillWidth: true
                                            Ui.PanelSlider {
                                                id: slider
                                                Layout.fillWidth: true
                                                bar: root.bar
                                                minimum: 0
                                                maximum: 100
                                                step: 1
                                                integer: true
                                                value: Math.min(100, row.current ?? 0)
                                                onReleased: function(value) { row.submit(Math.round(value)) }
                                            }
                                            Controls.AbstractButton {
                                                id: muteButton
                                                readonly property bool microphone: deviceCard.deviceRole === "source"
                                                readonly property bool muted: !!row.spec.muted
                                                readonly property color iconColor: muted ? "#ef5350" : root.panelForeground
                                                readonly property string actionLabel: strings.t(microphone
                                                    ? (muted ? "unmuteMicrophone" : "muteMicrophone")
                                                    : (muted ? "unmuteAudio" : "muteAudio"))
                                                implicitWidth: Commons.Style.space(30)
                                                implicitHeight: Commons.Style.space(30)
                                                hoverEnabled: true
                                                focusPolicy: Qt.StrongFocus
                                                Accessible.name: actionLabel
                                                Controls.ToolTip.text: actionLabel
                                                Controls.ToolTip.visible: hovered
                                                Controls.ToolTip.delay: 450
                                                onClicked: root.apply(deviceCard.deviceId, "mute", !muted)
                                                background: Rectangle {
                                                    radius: Commons.Style.cornerRadius
                                                    color: muteButton.hovered || muteButton.activeFocus
                                                        ? Qt.alpha(root.panelForeground, 0.12) : "transparent"
                                                    border.width: muteButton.activeFocus ? 1 : 0
                                                    border.color: Commons.Color.accent
                                                }
                                                contentItem: Item {
                                                    Shape {
                                                        anchors.centerIn: parent
                                                        width: 20
                                                        height: 20
                                                        scale: Commons.Style.space(20) / 20
                                                        ShapePath {
                                                            fillColor: "transparent"
                                                            strokeColor: muteButton.iconColor
                                                            strokeWidth: 1.5
                                                            capStyle: ShapePath.RoundCap
                                                            joinStyle: ShapePath.RoundJoin
                                                            PathSvg {
                                                                path: muteButton.microphone
                                                                    ? "M 7,4 A 3,3 0 0 1 13,4 V 9 A 3,3 0 0 1 7,9 Z M 4.5,9 A 5.5,5.5 0 0 0 15.5,9 M 10,14.5 V 18 M 7,18 H 13"
                                                                    : "M 2,7 H 6 L 10,3 V 17 L 6,13 H 2 Z M 13,7 Q 16,10 13,13 M 15.5,4.5 Q 21,10 15.5,15.5"
                                                            }
                                                        }
                                                        ShapePath {
                                                            fillColor: "transparent"
                                                            strokeColor: muteButton.muted ? Commons.Color.background : "transparent"
                                                            strokeWidth: 4
                                                            capStyle: ShapePath.RoundCap
                                                            PathSvg { path: "M 2,2 L 18,18" }
                                                        }
                                                        ShapePath {
                                                            fillColor: "transparent"
                                                            strokeColor: muteButton.muted ? muteButton.iconColor : "transparent"
                                                            strokeWidth: 1.8
                                                            capStyle: ShapePath.RoundCap
                                                            PathSvg { path: "M 2,2 L 18,18" }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.type === "slider"
                                            text: strings.t(row.spec.muted ? "muted" : "volume").arg(
                                                Commons.I18n.formatNumber(slider.dragging ? slider.liveValue : (row.current ?? 0)))
                                        }
                                        Plain {
                                            Layout.fillWidth: true
                                            visible: row.spec.type === "unavailable"
                                            text: row.spec.error || ""
                                        }
                                    }
                                }
                                Ui.Button {
                                    Layout.columnSpan: settingsGrid.columns
                                    foreground: root.panelForeground
                                    fontFamily: root.panelFont
                                    Layout.fillWidth: true
                                    visible: deviceCard.settingsOpen && deviceCard.hitsKeys.length > 0
                                    text: strings.t("hitsTitle")
                                    iconText: deviceCard.hitsOpen ? "⌄" : "›"
                                    focusable: true
                                    onClicked: deviceCard.hitsOpen = !deviceCard.hitsOpen
                                }
                                Plain {
                                    Layout.columnSpan: settingsGrid.columns
                                    Layout.fillWidth: true
                                    visible: deviceCard.settingsOpen && deviceCard.hitsOpen && deviceCard.hitsKeys.length > 0
                                    opacity: 0.75
                                    text: strings.t("hitsHint")
                                }
                                Repeater {
                                    model: deviceCard.hitsKeys
                                    ColumnLayout {
                                        id: triggerRow
                                        required property string modelData
                                        readonly property var spec: deviceCard.device?.controls.find(control => control.key === modelData)
                                        Layout.fillWidth: true
                                        visible: deviceCard.settingsOpen && deviceCard.hitsOpen && !!spec
                                        enabled: root.pending === null

                                        Plain {
                                            Layout.fillWidth: true
                                            text: triggerRow.spec ? strings.t(triggerRow.spec.button === 0 ? "hitsLeft" : "hitsRight")
                                                .arg(strings.t("hits." + triggerRow.spec.field))
                                                .arg(Math.round(triggerSlider.dragging ? triggerSlider.liveValue : triggerSlider.value)) : ""
                                        }
                                        Ui.PanelSlider {
                                            id: triggerSlider
                                            Layout.fillWidth: true
                                            bar: root.bar
                                            minimum: triggerRow.spec?.min ?? 0
                                            maximum: triggerRow.spec?.max ?? 1
                                            step: 1
                                            integer: true
                                            value: triggerRow.spec?.value ?? minimum
                                            onReleased: function(value) {
                                                root.apply(deviceCard.deviceId, triggerRow.modelData, Math.round(value))
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
