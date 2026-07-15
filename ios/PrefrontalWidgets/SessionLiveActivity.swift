import ActivityKit
import WidgetKit
import SwiftUI

/// Lock Screen + Dynamic Island presentation for the outing/focus/task Live
/// Activity (#466, M2). The clock is self-ticking — `Text(timerInterval:)`
/// counts an outing down to its back-by time; `Text(_, style: .timer)` counts a
/// focus session or a started task up — so it stays live with no push updates.
/// Started/ended by `LiveActivityManager`.
struct SessionLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: SessionActivityAttributes.self) { context in
            SessionLockScreenView(context: context)
                .activityBackgroundTint(.black.opacity(0.45))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    sessionIcon(context).font(.title3).foregroundStyle(.white)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    sessionTimer(context).font(.title3.monospacedDigit().weight(.semibold))
                        .foregroundStyle(.white).frame(maxWidth: 90)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(spacing: 1) {
                        Text(context.attributes.noun.uppercased())
                            .font(.caption2).foregroundStyle(.white.opacity(0.7))
                        Text(context.attributes.title).font(.caption.weight(.medium))
                            .foregroundStyle(.white).lineLimit(1)
                    }
                }
            } compactLeading: {
                sessionIcon(context)
            } compactTrailing: {
                sessionTimer(context).monospacedDigit().frame(maxWidth: 54)
            } minimal: {
                sessionIcon(context)
            }
        }
    }
}

private struct SessionLockScreenView: View {
    let context: ActivityViewContext<SessionActivityAttributes>
    var body: some View {
        HStack(spacing: 12) {
            sessionIcon(context).font(.title2).foregroundStyle(.white)
            VStack(alignment: .leading, spacing: 2) {
                Text(context.attributes.noun)
                    .font(.caption2).foregroundStyle(.white.opacity(0.7))
                Text(context.attributes.title)
                    .font(.headline).foregroundStyle(.white).lineLimit(1)
            }
            Spacer(minLength: 8)
            sessionTimer(context)
                .font(.title.monospacedDigit().weight(.semibold)).foregroundStyle(.white)
        }
        .padding()
    }
}

private func sessionIcon(_ c: ActivityViewContext<SessionActivityAttributes>) -> Image {
    Image(systemName: c.attributes.symbolName)
}

@ViewBuilder
private func sessionTimer(_ c: ActivityViewContext<SessionActivityAttributes>) -> some View {
    if c.attributes.countsDown, let ends = c.state.endsAt {
        // Fixed start…end range (start < end) so the interval is always valid;
        // countsDown shows time remaining to the back-by moment.
        Text(timerInterval: c.state.startedAt...ends, countsDown: true)
    } else {
        Text(c.state.startedAt, style: .timer)   // focus/task: elapsed, counting up
    }
}
