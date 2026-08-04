package com.ptransfer.companion

import android.app.Activity
import android.app.role.RoleManager
import android.content.ContentValues
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.CallLog
import android.provider.Telephony
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import java.io.File

/**
 * Writes messages and call-log entries back onto the phone.
 *
 * Android only permits writes to the SMS provider from whichever app currently holds the
 * default-SMS role, so the flow is: ask for the role, import, hand the role straight back.
 * The app makes that explicit rather than quietly keeping it.
 */
class ImportActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var action: Button
    private var kind: String = "sms"
    private var file: File? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        kind = intent.getStringExtra("kind") ?: "sms"
        file = intent.getStringExtra("file")?.let { File(it) }

        status = TextView(this).apply { setPadding(48, 48, 48, 24) }
        action = Button(this).apply { text = getString(R.string.import_start) }
        setContentView(
            LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                addView(status)
                addView(action)
            }
        )

        val source = file
        if (source == null || !source.exists()) {
            status.text = getString(R.string.import_missing_file, intent.getStringExtra("file") ?: "-")
            action.isEnabled = false
            return
        }

        status.text = getString(R.string.import_ready, kind, source.path)
        action.setOnClickListener { start() }
    }

    private fun start() {
        when (kind) {
            "sms" -> if (hasSmsRole()) importSms() else requestSmsRole()
            "calls" -> importCalls()
            "contacts" -> openVcard()
            else -> status.text = getString(R.string.import_unknown_kind, kind)
        }
    }

    // --- SMS ----------------------------------------------------------
    private fun hasSmsRole(): Boolean =
        packageName == Telephony.Sms.getDefaultSmsPackage(this)

    private fun requestSmsRole() {
        status.text = getString(R.string.import_need_role)
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            getSystemService(RoleManager::class.java)
                .createRequestRoleIntent(RoleManager.ROLE_SMS)
        } else {
            @Suppress("DEPRECATION")
            Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT)
                .putExtra(Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, packageName)
        }
        startActivityForResult(intent, REQUEST_SMS_ROLE)
    }

    override fun onActivityResult(request: Int, result: Int, data: Intent?) {
        super.onActivityResult(request, result, data)
        if (request == REQUEST_SMS_ROLE) {
            if (hasSmsRole()) importSms() else status.text = getString(R.string.import_role_refused)
        }
    }

    private fun importSms() {
        val source = file ?: return
        val entries = JSONArray(source.readText())
        var written = 0
        var skipped = 0

        for (i in 0 until entries.length()) {
            val m = entries.optJSONObject(i) ?: continue
            val date = m.optLong("date")
            val address = m.optString("address")
            val body = m.optString("body")
            if (alreadyPresent(address, date, body)) {
                skipped++
                continue
            }
            val values = ContentValues().apply {
                put(Telephony.Sms.ADDRESS, address)
                put(Telephony.Sms.BODY, body)
                put(Telephony.Sms.DATE, date)
                put(Telephony.Sms.DATE_SENT, m.optLong("date_sent"))
                put(Telephony.Sms.TYPE, m.optInt("type", Telephony.Sms.MESSAGE_TYPE_INBOX))
                put(Telephony.Sms.READ, m.optInt("read", 1))
            }
            runCatching { contentResolver.insert(Telephony.Sms.CONTENT_URI, values) }
                .onSuccess { if (it != null) written++ }
        }

        status.text = getString(R.string.import_sms_done, written, skipped)
        action.text = getString(R.string.import_hand_back)
        action.setOnClickListener { handBackSmsRole() }
    }

    /** Re-running an import must not double every conversation. */
    private fun alreadyPresent(address: String, date: Long, body: String): Boolean {
        if (date == 0L) return false
        return contentResolver.query(
            Telephony.Sms.CONTENT_URI,
            arrayOf(Telephony.Sms._ID),
            "${Telephony.Sms.DATE} = ? AND ${Telephony.Sms.ADDRESS} = ? AND ${Telephony.Sms.BODY} = ?",
            arrayOf(date.toString(), address, body),
            null,
        )?.use { it.count > 0 } ?: false
    }

    private fun handBackSmsRole() {
        status.text = getString(R.string.import_hand_back_hint)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startActivity(Intent(android.provider.Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS))
        }
    }

    // --- call log -----------------------------------------------------
    private fun importCalls() {
        val source = file ?: return
        val entries = JSONArray(source.readText())
        var written = 0
        for (i in 0 until entries.length()) {
            val c = entries.optJSONObject(i) ?: continue
            val values = ContentValues().apply {
                put(CallLog.Calls.NUMBER, c.optString("number"))
                put(CallLog.Calls.CACHED_NAME, c.optString("name"))
                put(CallLog.Calls.DATE, c.optLong("date"))
                put(CallLog.Calls.DURATION, c.optLong("duration"))
                put(CallLog.Calls.TYPE, c.optInt("type", CallLog.Calls.INCOMING_TYPE))
                put(CallLog.Calls.NEW, 0)
            }
            runCatching { contentResolver.insert(CallLog.Calls.CONTENT_URI, values) }
                .onSuccess { if (it != null) written++ }
        }
        status.text = getString(R.string.import_calls_done, written)
    }

    // --- contacts -----------------------------------------------------
    private fun openVcard() {
        val source = file ?: return
        val uri = Uri.fromFile(source)
        startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "text/x-vcard")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        )
    }

    companion object {
        private const val REQUEST_SMS_ROLE = 2001
    }
}
