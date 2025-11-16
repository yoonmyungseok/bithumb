package org.study.privateKey;

import com.auth0.jwt.JWT;
import com.auth0.jwt.algorithms.Algorithm;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import okhttp3.*;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.CrossOrigin;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.study.GlobalConfig;
import org.study.publicKey.MarketCode;

import java.util.List;
import java.util.UUID;

/**
 * 1. ClassName     : PrivateController
 * 2. FileName      : PrivateController.java
 * 3. Package       : org.study.privateKey
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 16. 오후 7:49
 * 6. Modified Date : 25. 11. 16. 오후 7:49
 * 7. Comment       :
 */
@RestController
@RequestMapping("private")
@Slf4j
@CrossOrigin(origins = "http://localhost:5173")
@RequiredArgsConstructor
public class PrivateController {
    
    private final PrivateService privateService;
    
    @GetMapping("/accounts")
    public ResponseEntity<List<AccountsDto>> 전체계좌조회(){
        return ResponseEntity.ok(privateService.전체계좌조회());
    }
}
