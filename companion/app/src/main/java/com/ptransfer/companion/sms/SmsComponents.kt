package com.ptransfer.companion.sms

import android.app.Activity
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.IBinder
import android.widget.TextView

/**
 * The four components Android demands of anything that can hold the default-SMS role.
 *
 * The companion only wants the role for the length of an import, so these are deliberately
 * inert: it must not start behaving like a messaging app while it holds the role, and it
 * must not silently swallow incoming messages.
 */

class SmsDeliverReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        // Intentionally does nothing. The companion is not a messaging app; hand the role
        // back to your real one as soon as the import finishes.
    }
}

class MmsDeliverReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) = Unit
}

class HeadlessSmsSendService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null
}

class ComposeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(
            TextView(this).apply {
                setPadding(48, 48, 48, 48)
                text = getString(com.ptransfer.companion.R.string.compose_not_a_messenger)
            }
        )
    }
}
