# Use the TeamUp Admin Console

[简体中文](admin-console.zh-CN.md)

## Sign in and check the Server

1. Connect to your TeamUp Server. For the SSH pilot, keep the [SSH tunnel](../modules/team-pilot.md#connect-and-install-the-client) running. For HTTPS, use the configured Server origin and a browser that trusts its certificate.
2. Open the Client Console and sign in with your TeamUp account. Select **Admin** just left of **Live**. On macOS you can also use **View → Admin**. Both open the Server Console in your browser.
3. Sign in there with the same TeamUp username and password. If you already have the pilot tunnel on port 18765, you can also [open the Server Console directly](http://127.0.0.1:18765/console/). For another Server, open `/console/` on its configured origin.
4. Select **Rooms** to check members and recent activity. Open **Timeline** for room events or **Chats** to read messages when you have permission for that room.
5. Select **Server Dashboard** to check system health, traffic, latency, and room load. Select a room in **Room load** to see its metrics. With log access, choose a window of up to one hour and select **Load events**.
6. Select **CI Dashboards** to see repository activity and CI results. Select **Sign out** when you're finished.

The Server checks your grants on each request. The Client's Admin entry appears for accounts with console access. Server Dashboard and Accounts require their own grants. If the Client's TeamUp session has expired, restart the local Client and sign in again. For SSH installations that forward a Unix socket, keep the browser's loopback TCP forward running too.

## Create an account

1. Open the **Accounts** tab and find the **Create account** form.
2. Choose a username with 3–64 lowercase letters, digits, underscores, or hyphens, starting with a letter.
3. Enter and confirm a temporary password of 12–128 characters. Select **Create account** and check that the username appears as **Active**.
4. Send the username and temporary password to the new user through your team's private credential-sharing channel. They can use them to sign in to their Client Console.

## Reset a password or disable an account

- Select **Reset password** on the account's row. Enter and confirm the new password, then save. The account's human sessions end. Its registered Agents keep working.
- Select **Disable** and confirm the username. Disabling is permanent and ends both human and Agent access. Existing room history still shows that account's members.

The Server's local account owner can bootstrap console and account-management grants. Once the grants are in place, the steps above run entirely in the browser. [Admin Console permissions](../modules/admin-console.md#accounts)
