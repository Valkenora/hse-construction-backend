WAH = 'working_at_height'
ENABLED_TYPES = (WAH,)

FORMS = {
    WAH: {
        'code': 'F.01/P.10/SHE/KPT',   # literal code printed on the client's paper form
        'revision': 1,
        'title': 'Ijin Kerja Di Ketinggian',
        'safety_areas': {
            'A': 'Area Perifer (Pinggiran)',
            'B': 'Area Pit Lift/ Tangga',
            'C': 'Pekerjaan perbaikan/ perawatan di ketinggian',
        },
        'checklist': dict(enumerate([
            'Apakah Surat Perintah Kerja (SPK) atau kontrak pekerjaan ini sudah ditandatangani?',
            'Apakah penanggungjawab pekerjaan sudah ditentukan?',
            'Apakah cuaca cerah dan tidak mendung?',
            'Apakah pekerja sudah memakai dan mempersiapkan APD yang sesuai?',
            'Apakah pekerja sudah mendapatkan pengarahan K3 (Safety Induction)?',
            'Apakah pekerja mempunyai pengalaman dan kompeten melakukan pekerjaan ini?',
            'Apakah pekerja tidak menderita penyakit ayan, rabun senja, atau gangguan keseimbangan?',
            'Apakah prosedur keadaan darurat sudah dimengerti?',
            'Apakah rambu-rambu yang diperlukan sudah terpasang?',
            'Apakah safety line, safety net, dan safety deck sudah terpasang?',
            'Apakah climbing sudah terpasang? (untuk pekerjaan corewall)',
            'Apakah area pekerjaan sudah dibersihkan dan area di bawahnya bebas dari area kerja dan lalu lintas orang?',
            'Apakah peralatan sudah diinspeksi dan layak pakai?',
            'Apakah bahan kimia B3 kemasannya memadai (bebas potensi bocor), sudah dilengkapi '
            'Material Safety Data Sheet (MSDS) dan diberi label yang jelas? (jika menggunakan bahan kimia B3)',
        ], start=1)),
        'ppe': {
            'sarung_tangan': 'Sarung tangan', 'safety_belt': 'Safety Belt',
            'body_harness': 'Body Harness', 'sepatu': 'Sepatu',
        },
        'ppe_other_max': 2,   # the two free-text lines on the form
    },
}
FORMS[WAH]['checklist'] = {str(k): v for k, v in FORMS[WAH]['checklist'].items()}