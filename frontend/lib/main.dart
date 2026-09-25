import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

void main() => runApp(const SecretCrawlerApp());

class SecretCrawlerApp extends StatelessWidget {
  const SecretCrawlerApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'SecretCrawler', theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true), home: const Dashboard());
}

class Dashboard extends StatefulWidget { const Dashboard({super.key}); @override State<Dashboard> createState() => _DashboardState(); }
class _DashboardState extends State<Dashboard> {
  String _status = 'Not connected';
  Future<void> _checkServer() async {
    try {
      final response = await http.get(Uri.parse('http://127.0.0.1:8080/health'));
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      setState(() => _status = data['status'] as String);
    } catch (_) { setState(() => _status = 'Server unavailable'); }
  }
  @override Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('SecretCrawler · Consent-first control plane')),
    body: ListView(padding: const EdgeInsets.all(20), children: [
      Text('Connection: $_status', style: Theme.of(context).textTheme.titleLarge), const SizedBox(height: 12),
      FilledButton.icon(onPressed: _checkServer, icon: const Icon(Icons.health_and_safety), label: const Text('Check local Rust service')),
      const SizedBox(height: 24),
      const _GuardrailCard(icon: Icons.privacy_tip_outlined, title: 'Privacy by default', text: 'No biometric collection, identity concealment, CAPTCHA solving, or third-party traffic inspection.'),
      const _GuardrailCard(icon: Icons.admin_panel_settings_outlined, title: 'Layered permissions', text: 'Owner, Operator, and Viewer roles follow least privilege. Sensitive operations require an approval request.'),
      const _GuardrailCard(icon: Icons.router_outlined, title: 'Virtual-network controls', text: 'Only user-owned endpoints, explicit consent, rate limits, and conservative connection limits are enabled.'),
    ]));
}
class _GuardrailCard extends StatelessWidget { final IconData icon; final String title; final String text; const _GuardrailCard({required this.icon, required this.title, required this.text});
 @override Widget build(BuildContext context) => Card(child: ListTile(leading: Icon(icon), title: Text(title), subtitle: Text(text)));
}
