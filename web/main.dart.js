import 'package:flutter/material.dart';

import 'api.dart';
import 'pages.dart';
import 'splash.dart';
import 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Api.init();
  runApp(const AttendanceApp());
}

class AttendanceApp extends StatelessWidget {
  const AttendanceApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'KARE Attendance System',
      debugShowCheckedModeBanner: false,
      theme: Kare.theme(),
      home: const SplashPage(),
    );
  }
}

void toast(BuildContext c, String msg) => ScaffoldMessenger.of(c).showSnackBar(
  SnackBar(content: Text(msg), duration: const Duration(seconds: 4)),
);

class LoginPage extends StatefulWidget {
  const LoginPage({super.key});
  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final id = TextEditingController(),
      name = TextEditingController(),
      faculty = TextEditingController(),
      pass = TextEditingController(),
      url = TextEditingController(text: Api.baseUrl);
  bool needsAdmin = false,
      busy = false; // needsAdmin: nobody created the head account yet

  @override
  void initState() {
    super.initState();
    checkSetup();
  }

  Future<void> checkSetup() async {
    try {
      final n = await Api.needsAdmin();
      if (mounted) setState(() => needsAdmin = n);
    } catch (_) {
      // the server is not reachable yet, so only the sign in form is shown
    }
  }

  Future<void> submit() async {
    setState(() => busy = true);
    try {
      await Api.setBaseUrl(url.text);
      if (needsAdmin) {
        await Api.signup(
          id.text.trim(),
          name.text.trim(),
          faculty.text.trim(),
          pass.text,
        );
      }
      await Api.login(id.text.trim(), pass.text);
      if (!mounted) return;
      Navigator.of(context)
          .pushReplacement(MaterialPageRoute(builder: (_) => const HomePage()));
    } on ApiError catch (e) {
      if (mounted) toast(context, e.message);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    Widget field(TextEditingController c, String label, {bool hide = false}) =>
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: TextField(
            controller: c,
            obscureText: hide,
            decoration: InputDecoration(labelText: label),
          ),
        );
    final t = Theme.of(context).textTheme;
    return Scaffold(
      body: KareBackdrop(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const ScanMark(size: 84),
                  const SizedBox(height: 18),
                  const KareTitle(scale: 0.62),
                  const SizedBox(height: 28),
                  Container(
                    padding: const EdgeInsets.all(24),
                    decoration: BoxDecoration(
                      color: Colors.white,
                      borderRadius: BorderRadius.circular(20),
                      boxShadow: const [
                        BoxShadow(
                          color: Color(0x40000000),
                          blurRadius: 30,
                          offset: Offset(0, 14),
                        ),
                      ],
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Text(
                          needsAdmin
                              ? 'Create head account'
                              : 'Faculty sign in',
                          style: t.titleLarge?.copyWith(
                            fontWeight: FontWeight.w800,
                            color: Kare.navy,
                          ),
                        ),
                        const SizedBox(height: 4),
                        Text(
                          needsAdmin
                              ? 'First time setup. This account can create logins for all faculty.'
                              : 'Sign in with the Gmail ID and password the head gave you.',
                          style: t.bodyMedium?.copyWith(color: Colors.black54),
                        ),
                        const SizedBox(height: 20),
                        field(id, 'Gmail ID'),
                        if (needsAdmin) field(name, 'Full name'),
                        if (needsAdmin)
                          field(faculty, 'Faculty / register number'),
                        field(pass, 'Password', hide: true),
                        const SizedBox(height: 4),
                        FilledButton(
                          onPressed: busy ? null : submit,
                          child: Padding(
                            padding: const EdgeInsets.all(12),
                            child: busy
                                ? const SizedBox(
                                    height: 18,
                                    width: 18,
                                    child: CircularProgressIndicator(
                                      strokeWidth: 2,
                                      color: Colors.white,
                                    ),
                                  )
                                : Text(
                                    needsAdmin
                                        ? 'Create head account'
                                        : 'Sign in',
                                  ),
                          ),
                        ),
                        ExpansionTile(
                          shape: const Border(),
                          collapsedShape: const Border(),
                          tilePadding: EdgeInsets.zero,
                          title: const Text(
                            'Server address',
                            style: TextStyle(fontSize: 14),
                          ),
                          childrenPadding: const EdgeInsets.only(bottom: 8),
                          children: [
                            field(url, 'Server URL'),
                            TextButton(
                              onPressed: () async {
                                await Api.setBaseUrl(url.text);
                                await checkSetup();
                                if (context.mounted)
                                  toast(context, 'Server checked.');
                              },
                              child: const Text('Check server'),
                            ),
                          ],
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
