package com.ptransfer.companion

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.CallLog
import android.provider.ContactsContract
import android.provider.Telephony
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Exports contacts, messages and the call log to files the desktop app collects.
 *
 * This exists for phones whose build refuses to let the `adb shell` user read those
 * providers - increasingly common on Android 14+ OEM ROMs. An app the user installed and
 * granted permissions to is allowed where the shell is not.
 */
class ExportActivity : Activity() {

    private lateinit var status: TextView

    private val permissions = arrayOf(
        Manifest.permission.READ_CONTACTS,
        Manifest.permission.READ_SMS,
        Manifest.permission.READ_CALL_LOG,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        status = TextView(this).apply { setPadding(48, 48, 48, 48) }
        setContentView(LinearLayout(this).apply { addView(status) })

        if (permissions.any { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }) {
            requestPermissions(permissions, REQUEST_CODE)
        } else {
            export()
        }
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, perms, results)
        if (code == REQUEST_CODE) export()
    }

    private fun export() {
        val outDir = File(intent.getStringExtra("out") ?: "${externalCacheDir?.path}/ptransfer-export")
        outDir.mkdirs()
        val lines = mutableListOf<String>()

        runCatching { File(outDir, "contacts.vcf").writeText(exportContacts()) }
            .onSuccess { lines += "contacts exported" }
            .onFailure { lines += "contacts failed: ${it.message}" }

        runCatching { File(outDir, "sms.json").writeText(exportSms()) }
            .onSuccess { lines += "messages exported" }
            .onFailure { lines += "messages failed: ${it.message}" }

        runCatching { File(outDir, "calls.json").writeText(exportCalls()) }
            .onSuccess { lines += "call log exported" }
            .onFailure { lines += "call log failed: ${it.message}" }

        status.text = "Exported to:\n${outDir.path}\n\n${lines.joinToString("\n")}"
    }

    private fun exportContacts(): String {
        val out = StringBuilder()
        contentResolver.query(
            ContactsContract.Contacts.CONTENT_URI,
            arrayOf(ContactsContract.Contacts.LOOKUP_KEY),
            null, null, null,
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val lookup = cursor.getString(0) ?: continue
                val uri = ContactsContract.Contacts.CONTENT_VCARD_URI.buildUpon()
                    .appendPath(lookup).build()
                contentResolver.openInputStream(uri)?.use { stream ->
                    out.append(stream.readBytes().toString(Charsets.UTF_8))
                    if (!out.endsWith("\n")) out.append("\n")
                }
            }
        }
        return out.toString()
    }

    private fun exportSms(): String {
        val messages = JSONArray()
        contentResolver.query(Telephony.Sms.CONTENT_URI, null, null, null, "${Telephony.Sms.DATE} ASC")
            ?.use { cursor ->
                while (cursor.moveToNext()) {
                    messages.put(
                        JSONObject().apply {
                            put("address", cursor.stringOf(Telephony.Sms.ADDRESS))
                            put("body", cursor.stringOf(Telephony.Sms.BODY))
                            put("date", cursor.longOf(Telephony.Sms.DATE))
                            put("date_sent", cursor.longOf(Telephony.Sms.DATE_SENT))
                            put("type", cursor.longOf(Telephony.Sms.TYPE))
                            put("read", cursor.longOf(Telephony.Sms.READ))
                            put("thread_id", cursor.longOf(Telephony.Sms.THREAD_ID))
                        }
                    )
                }
            }
        return messages.toString(1)
    }

    private fun exportCalls(): String {
        val calls = JSONArray()
        contentResolver.query(CallLog.Calls.CONTENT_URI, null, null, null, "${CallLog.Calls.DATE} ASC")
            ?.use { cursor ->
                while (cursor.moveToNext()) {
                    calls.put(
                        JSONObject().apply {
                            put("number", cursor.stringOf(CallLog.Calls.NUMBER))
                            put("name", cursor.stringOf(CallLog.Calls.CACHED_NAME))
                            put("date", cursor.longOf(CallLog.Calls.DATE))
                            put("duration", cursor.longOf(CallLog.Calls.DURATION))
                            put("type", cursor.longOf(CallLog.Calls.TYPE))
                        }
                    )
                }
            }
        return calls.toString(1)
    }

    companion object {
        private const val REQUEST_CODE = 1001
    }
}

internal fun android.database.Cursor.stringOf(column: String): String {
    val index = getColumnIndex(column)
    return if (index >= 0) getString(index) ?: "" else ""
}

internal fun android.database.Cursor.longOf(column: String): Long {
    val index = getColumnIndex(column)
    return if (index >= 0) getLong(index) else 0L
}
