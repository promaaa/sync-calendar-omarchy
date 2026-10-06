import QtQuick
import qs.Commons

// A rounded option button, filled with the accent color while selected.
Rectangle {
  id: pill

  property string text: ""
  property bool selected: false
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  signal clicked()

  width: label.implicitWidth + Style.space(14)
  height: Style.space(22)
  radius: Style.cornerRadius > 0 ? height / 2 : 0
  color: selected ? Color.accent : "transparent"
  border.width: 1
  border.color: selected ? Color.accent : Qt.darker(foreground, 1.8)

  Text {
    id: label
    anchors.centerIn: parent
    textFormat: Text.PlainText
    text: pill.text
    color: pill.selected ? Color.background : pill.foreground
    font.family: pill.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: pill.selected
  }

  MouseArea {
    anchors.fill: parent
    cursorShape: Qt.PointingHandCursor
    onClicked: pill.clicked()
  }
}
